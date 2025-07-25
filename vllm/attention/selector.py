# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import os
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from typing import Generator, Optional, Union

import torch

import vllm.envs as envs
from vllm.attention.backends.abstract import AttentionBackend
from vllm.logger import init_logger
from vllm.platforms import _Backend, current_platform
from vllm.utils import STR_BACKEND_ENV_VAR, resolve_obj_by_qualname

logger = init_logger(__name__)


def backend_name_to_enum(backend_name: str) -> Optional[_Backend]:
    """
    Convert a string backend name to a _Backend enum value.

    Returns:
    * _Backend: enum value if backend_name is a valid in-tree type
    * None: otherwise it's an invalid in-tree type or an out-of-tree platform is
            loaded.
    """
    assert backend_name is not None
    return _Backend[backend_name] if backend_name in _Backend.__members__ else \
          None


def get_env_variable_attn_backend() -> Optional[_Backend]:
    '''
    Get the backend override specified by the vLLM attention
    backend environment variable, if one is specified.

    Returns:

    * _Backend enum value if an override is specified
    * None otherwise
    '''
    backend_name = os.environ.get(STR_BACKEND_ENV_VAR)
    return (None
            if backend_name is None else backend_name_to_enum(backend_name))


# Global state allows a particular choice of backend
# to be forced, overriding the logic which auto-selects
# a backend based on system & workload configuration
# (default behavior if this variable is None)
#
# THIS SELECTION TAKES PRECEDENCE OVER THE
# VLLM_ATTENTION_BACKEND ENVIRONMENT VARIABLE
forced_attn_backend: Optional[_Backend] = None


def global_force_attn_backend(attn_backend: Optional[_Backend]) -> None:
    '''
    Force all attention operations to use a specified backend.

    Passing `None` for the argument re-enables automatic
    backend selection.,

    Arguments:

    * attn_backend: backend selection (None to revert to auto)
    '''
    global forced_attn_backend
    forced_attn_backend = attn_backend


def get_global_forced_attn_backend() -> Optional[_Backend]:
    '''
    Get the currently-forced choice of attention backend,
    or None if auto-selection is currently enabled.
    '''
    return forced_attn_backend


@dataclass(frozen=True)
class _IsSupported:
    can_import: bool
    head_size: bool
    dtype: bool

    def __bool__(self) -> bool:
        return self.can_import and self.head_size and self.dtype


def is_attn_backend_supported(
    attn_backend: Union[str, type[AttentionBackend]],
    head_size: int,
    dtype: torch.dtype,
    *,
    allow_import_error: bool = True,
) -> _IsSupported:
    if isinstance(attn_backend, str):
        try:
            attn_backend = resolve_obj_by_qualname(attn_backend)
        except ImportError:
            if not allow_import_error:
                raise

            return _IsSupported(can_import=False, head_size=False, dtype=False)

    assert isinstance(attn_backend, type)

    # TODO: Update the interface once V0 is removed
    if get_supported_head_sizes := getattr(attn_backend,
                                           "get_supported_head_sizes", None):
        is_head_size_supported = head_size in get_supported_head_sizes()
    elif validate_head_size := getattr(attn_backend, "validate_head_size",
                                       None):
        try:
            validate_head_size(head_size)
            is_head_size_supported = True
        except Exception:
            is_head_size_supported = False
    else:
        raise NotImplementedError(f"{attn_backend.__name__} does not support "
                                  "head size validation")

    if get_supported_dtypes := getattr(attn_backend, "get_supported_dtypes",
                                       None):
        is_dtype_supported = dtype in get_supported_dtypes()
    else:
        raise NotImplementedError(f"{attn_backend.__name__} does not support "
                                  "dtype validation")

    return _IsSupported(
        can_import=True,
        head_size=is_head_size_supported,
        dtype=is_dtype_supported,
    )


def get_attn_backend(
    head_size: int,
    dtype: torch.dtype,
    kv_cache_dtype: Optional[str],
    block_size: int,
    is_attention_free: bool,
    use_mla: bool = False,
) -> type[AttentionBackend]:
    """Selects which attention backend to use and lazily imports it."""
    # Accessing envs.* behind an @lru_cache decorator can cause the wrong
    # value to be returned from the cache if the value changes between calls.
    # To avoid this, we read envs.VLLM_USE_V1 here and pass it explicitly to the
    # private function.
    return _cached_get_attn_backend(
        head_size=head_size,
        dtype=dtype,
        kv_cache_dtype=kv_cache_dtype,
        block_size=block_size,
        is_attention_free=is_attention_free,
        use_v1=envs.VLLM_USE_V1,
        use_mla=use_mla,
    )


@cache
def _cached_get_attn_backend(
    head_size: int,
    dtype: torch.dtype,
    kv_cache_dtype: Optional[str],
    block_size: int,
    is_attention_free: bool,
    use_v1: bool = False,
    use_mla: bool = False,
) -> type[AttentionBackend]:
    # If there are no attention layers (e.g. we are running Mamba),
    # use the placeholder NO_ATTENTION
    if is_attention_free:
        from vllm.attention.backends.placeholder_attn import (
            PlaceholderAttentionBackend)
        return PlaceholderAttentionBackend

    # Check whether a particular choice of backend was
    # previously forced.
    #
    # THIS SELECTION OVERRIDES THE VLLM_ATTENTION_BACKEND
    # ENVIRONMENT VARIABLE.
    selected_backend = None
    backend_by_global_setting: Optional[_Backend] = (
        get_global_forced_attn_backend())
    if backend_by_global_setting is not None:
        selected_backend = backend_by_global_setting
    else:
        # Check the environment variable and override if specified
        backend_by_env_var: Optional[str] = envs.VLLM_ATTENTION_BACKEND
        if backend_by_env_var is not None:
            selected_backend = backend_name_to_enum(backend_by_env_var)

    # get device-specific attn_backend
    attention_cls = current_platform.get_attn_backend_cls(
        selected_backend, head_size, dtype, kv_cache_dtype, block_size, use_v1,
        use_mla)
    if not attention_cls:
        raise ValueError(
            f"Invalid attention backend for {current_platform.device_name}")
    return resolve_obj_by_qualname(attention_cls)


@contextmanager
def global_force_attn_backend_context_manager(
        attn_backend: _Backend) -> Generator[None, None, None]:
    '''
    Globally force a vLLM attention backend override within a
    context manager, reverting the global attention backend
    override to its prior state upon exiting the context
    manager.

    Arguments:

    * attn_backend: attention backend to force

    Returns:

    * Generator
    '''

    # Save the current state of the global backend override (if any)
    original_value = get_global_forced_attn_backend()

    # Globally force the new backend override
    global_force_attn_backend(attn_backend)

    # Yield control back to the enclosed code block
    try:
        yield
    finally:
        # Revert the original global backend override, if any
        global_force_attn_backend(original_value)
