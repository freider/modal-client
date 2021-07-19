import asyncio
import concurrent.futures
import hashlib
import os
import sys
import time

from .async_utils import retry, synchronizer
from .config import logger
from .function import decorate_function
from .grpc_utils import GRPC_REQUEST_TIMEOUT, BLOCKING_REQUEST_TIMEOUT
from .proto import api_pb2


def get_sha256_hex(filename, rel_filename):
    # Somewhat CPU intensive, so we run it in a thread/process
    m = hashlib.sha256()
    m.update(open(filename, 'rb').read())
    return filename, rel_filename, m.hexdigest()


async def get_files(local_dir, condition):
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as exe:
        futs = []
        for root, dirs, files in os.walk(local_dir):
            for name in files:
                filename = os.path.join(root, name)
                rel_filename = os.path.relpath(filename, local_dir)
                if condition(filename):
                    futs.append(loop.run_in_executor(exe, get_sha256_hex, filename, rel_filename))
        logger.debug(f'Computing checksums for {len(futs)} files using {exe._max_workers} workers')
        for fut in asyncio.as_completed(futs):
            filename, rel_filename, sha256_hex = await fut
            yield filename, rel_filename, sha256_hex


@synchronizer
class Mount:
    def __init__(self, local_dir, remote_dir, condition):
        self.local_dir = local_dir
        self.remote_dir = remote_dir
        self.mount_id = None
        self.condition = condition
        self._hashes = {}
        self._remote_to_local_filename = {}

    async def _register_file_requests(self, mount_id):
        async for filename, rel_filename, sha256_hex in get_files(self.local_dir, self.condition):
            remote_filename = os.path.join(self.remote_dir, rel_filename)  # won't work on windows
            self._remote_to_local_filename[remote_filename] = filename
            request = api_pb2.MountRegisterFileRequest(filename=remote_filename, sha256_hex=sha256_hex, mount_id=mount_id)
            self._hashes[filename] = sha256_hex
            yield request

    async def _upload_file_requests(self, client, mount_id):
        t0 = time.time()
        n_files, n_missing_files, total_bytes = 0, 0, 0
        async for response in client.stub.MountRegisterFile(self._register_file_requests(mount_id)):
            n_files += 1
            if not response.exists:
                filename = self._remote_to_local_filename[response.filename]
                data = open(filename, 'rb').read()
                n_missing_files += 1
                total_bytes += len(data)
                logger.debug(f'Uploading file {filename} to {response.filename} ({len(data)} bytes)')
                request = api_pb2.MountUploadFileRequest(data=data, sha256_hex=self._hashes[filename], size=len(data), mount_id=mount_id)
                yield request

        logger.info(f'Uploaded {n_missing_files}/{n_files} files and {total_bytes} bytes in {time.time() - t0}s')

    async def start(self, client):
        # TODO: I think in theory we could split the get_files iterator and launch multiple concurrent
        # calls to MountRegisterFileRequest and MountUploadFileRequest. This would speed up a lot of the
        # serial operations on the server side (like hitting Redis for every file serially).
        # Another option is to parallelize more on the server side.

        if self.mount_id:
            return self.mount_id

        req = api_pb2.MountCreateRequest(client_id=client.client_id)
        resp = await client.stub.MountCreate(req)
        mount_id = resp.mount_id

        logger.debug(f'Uploading mount {mount_id}')
        await client.stub.MountUploadFile(self._upload_file_requests(client, mount_id))

        req = api_pb2.MountDoneRequest(mount_id=mount_id)
        await client.stub.MountDone(req)

        self.mount_id = mount_id
        return self.mount_id


# The default mount will upload all Python files in the current workdir into /root
mount_py_in_workdir_into_root = Mount(
    '.',
    '/root',
    lambda filename: os.path.splitext(filename)[-1] == '.py'
)

@synchronizer
class Layer:
    def __init__(self, tag=None, base_layers={}, dockerfile_commands=[], context_files={}):
        self.layer_id = None
        self.tag = tag
        self.base_layers = base_layers
        self.dockerfile_commands = dockerfile_commands
        self.context_files = context_files

    async def start(self, client):  # Note that we join on an image level
        # TODO: there's some risk of a race condition here
        if self.layer_id is not None:
            return self.layer_id

        base_layers = []
        for docker_tag, layer in self.base_layers.items():
            layer_id = await layer.start(client)
            # TODO: we should make sure this layer actually gets built
            base_layers.append(api_pb2.BaseLayer(
                docker_tag=docker_tag,
                layer_id=layer_id
            ))

        context_files = [
            api_pb2.LayerContextFile(filename=filename, data=data)
            for filename, data in self.context_files.items()
        ]

        layer_definition = api_pb2.Layer(
            tag=self.tag,
            base_layers=base_layers,
            dockerfile_commands=self.dockerfile_commands,
            context_files=context_files,
        )

        request = api_pb2.LayerCreateRequest(
            client_id=client.client_id,
            layer=layer_definition
        )
        response = await client.stub.LayerCreate(request)
        self.layer_id = response.layer_id
        return self.layer_id


@synchronizer
class Image:
    def __init__(self, layer, mounts=[]):
        self.layer = layer
        self.mounts = mounts
        self.image_id = None

    async def start(self, client):
        # TODO: there's some risk of a race condition here
        if self.image_id is not None:
            return self.image_id

        layer_id = await self.layer.start(client)
        mount_ids = []
        for mount in self.mounts:
            mount_ids.append(await mount.start(client))

        image = api_pb2.Image(layer_id=self.layer.layer_id, mount_ids=mount_ids)

        response = await client.stub.ImageCreate(api_pb2.ImageCreateRequest(client_id=client.client_id, image=image))
        self.image_id = response.image_id
        return self.image_id

    async def join(self, client):
        logger.debug('Waiting for image %s' % self.image_id)
        while True:
            request = api_pb2.ImageJoinRequest(image_id=self.image_id, timeout=BLOCKING_REQUEST_TIMEOUT)
            response = await retry(client.stub.ImageJoin)(request, timeout=GRPC_REQUEST_TIMEOUT)
            if not response.result.status:
                continue
            elif response.result.status == api_pb2.GenericResult.Status.FAILURE:
                raise Exception(response.result.exception)
            elif response.result.status == api_pb2.GenericResult.Status.SUCCESS:
                return response
            else:
                raise Exception('Unknown status %s!' % response.result.status)

    def function(self, raw_f):
        ''' Primarily to be used as a decorator.'''
        return decorate_function(raw_f, self)


class DebianSlim(Image):
    def __init__(self, layer=None, python_version=None):
        if python_version is None:
            python_version = '%d.%d.%d' % sys.version_info[:3]
        self.python_version=python_version
        if layer is None:
            layer = Layer(tag='python-%s-slim-buster-base' % self.python_version)
        super().__init__(layer=layer, mounts=[mount_py_in_workdir_into_root])

    def add_python_packages(self, python_packages):
        layer = Layer(
            base_layers={
                'base': self.layer,
                'builder': Layer(tag='python-%s-slim-buster-builder' % self.python_version)
            },
            dockerfile_commands=[
                'FROM builder as builder-vehicle',
                'RUN pip wheel %s -w /tmp/wheels' % ' '.join(python_packages),
                'FROM base',
                'COPY --from=builder-vehicle /tmp/wheels /tmp/wheels',
                'RUN pip install /tmp/wheels/*',
                'RUN rm -rf /tmp/wheels',
            ]
        )
        return DebianSlim(layer=layer)

    def run_commands(self, commands):
        layer = Layer(
            base_layers={'base': self.layer},
            dockerfile_commands=['FROM base'] + ['RUN ' + command for command in commands]
        )
        return DebianSlim(layer=layer)


debian_slim = DebianSlim()
base_image = debian_slim
