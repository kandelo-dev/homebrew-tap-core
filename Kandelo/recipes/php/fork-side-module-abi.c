#include "abi_constants.h"

/*
 * PHP extensions are dlopen side modules, so they must not link the process
 * syscall glue or a second libc. Export the ABI epoch required by Kandelo's
 * dynamic loader. Modules that import env.fork are instrumented after their
 * final shared link.
 */
__attribute__((used))
__attribute__((retain))
__attribute__((export_name("__abi_version")))
unsigned int __wasm_posix_user_abi_version(void) {
    return WASM_POSIX_ABI_VERSION;
}
