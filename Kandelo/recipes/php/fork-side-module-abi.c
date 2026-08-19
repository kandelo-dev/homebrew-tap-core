#include "abi_constants.h"

/*
 * Opcache is a dlopen side module, so it must not link the process syscall
 * glue or a second libc. Export the ABI epoch required by Kandelo's dynamic
 * loader without applying fork instrumentation to this fork-free module.
 */
__attribute__((used))
__attribute__((retain))
__attribute__((export_name("__abi_version")))
unsigned int __wasm_posix_user_abi_version(void) {
    return WASM_POSIX_ABI_VERSION;
}
