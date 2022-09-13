import asyncio
import time
import warnings

from modal import Stub

SLEEP_DELAY = 0.1

stub = Stub()


@stub.function()
def square(x):
    return x * x


@stub.function()
def delay(t):
    time.sleep(t)


@stub.function()
async def square_async(x):
    await asyncio.sleep(SLEEP_DELAY)
    return x * x


@stub.function()
def raises(x):
    raise Exception("Failure!")


def deprecated_function(x):
    warnings.warn("This function is deprecated", DeprecationWarning)
    return x**2


class Cube:
    _events = []

    def __init__(self):
        self._events.append("init")

    def __enter__(self):
        self._events.append("enter")

    def __exit__(self, typ, exc, tb):
        self._events.append("exit")

    @stub.function()
    def f(self, x):
        self._events.append("call")
        return x**3


class CubeAsync:
    _events = []

    def __init__(self):
        self._events.append("init")

    async def __aenter__(self):
        self._events.append("enter")

    async def __aexit__(self, typ, exc, tb):
        self._events.append("exit")

    @stub.function()
    async def f(self, x):
        self._events.append("call")
        return x**3


if __name__ == "__main__":
    raise Exception("This line is not supposed to be reachable")
