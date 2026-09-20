"""Native operation boundary shared by local builds and optional helpers."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect

_executor = contextvars.ContextVar("v2w_compute_executor", default=None)
_description = contextvars.ContextVar("v2w_operation_description", default="")
OPERATIONS = {}


@contextlib.contextmanager
def compute_with(executor, *, description=""):
    token = _executor.set(executor)
    label_token = _description.set(description)
    try:
        yield
    finally:
        _executor.reset(token)
        _description.reset(label_token)


def current_executor():
    return _executor.get()


def operation_description():
    return _description.get()


def operation(name, *, source="video", output=None, remote=True):
    def decorate(function):
        signature = inspect.signature(function)
        OPERATIONS[name] = dict(function=function, signature=signature,
                                source=source, output=output, remote=remote)

        @functools.wraps(function)
        def execute(*args, **kwargs):
            executor = _executor.get()
            if executor is None:
                return function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return executor.run(name, bound.arguments, lambda: function(*args, **kwargs))

        return execute
    return decorate
