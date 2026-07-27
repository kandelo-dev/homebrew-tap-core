/*
 * Feeds ICU its common data at intl.so load time.
 *
 * ICU ships as the standalone file icu.dat, but ICU's automatic loader only
 * looks for the conventional icudt<ver><endian>.dat name. Keeping the roughly
 * 30 MiB data file separate makes it a normal bottle member instead of
 * embedding it in intl.so. The side-module loader runs __wasm_call_ctors before
 * PHP calls intl's MINIT, so the data is ready before an ICU service uses it.
 *
 * A missing file remains loud but does not make merely loading intl.so fatal:
 * callers that actually use ICU receive its normal missing-resource failure.
 */

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <unicode/uclean.h>
#include <unicode/udata.h>
#include <unicode/utypes.h>

#ifndef KANDELO_ICU_DAT_PATH
#define KANDELO_ICU_DAT_PATH "/home/linuxbrew/.linuxbrew/opt/php/share/php/icu.dat"
#endif

static void kandelo_intl_load_icu_data(void) __attribute__((constructor));

static void kandelo_intl_load_icu_data(void) {
    const char *path = KANDELO_ICU_DAT_PATH;
    int fd = open(path, O_RDONLY);
    struct stat st;
    size_t size;
    size_t offset;
    void *buffer;
    UErrorCode status;

    if (fd < 0) {
        fprintf(stderr,
                "[intl] ICU data not loaded: cannot open %s. "
                "intl functions will fail with U_MISSING_RESOURCE_ERROR. "
                "Materialize the complete PHP bottle closure.\n",
                path);
        return;
    }

    if (fstat(fd, &st) != 0 || st.st_size <= 0) {
        fprintf(stderr, "[intl] ICU data not loaded: cannot stat %s.\n", path);
        close(fd);
        return;
    }

    /*
     * WHY: ICU retains this pointer for the process lifetime. The allocation
     * deliberately remains owned by ICU, and reading once per process avoids
     * relying on Kandelo's emulated mmap for immutable package data.
     */
    size = (size_t) st.st_size;
    buffer = malloc(size);
    if (buffer == NULL) {
        fprintf(stderr, "[intl] ICU data not loaded: OOM reading %s (%zu bytes).\n",
                path, size);
        close(fd);
        return;
    }

    offset = 0;
    while (offset < size) {
        ssize_t count = read(fd, (char *) buffer + offset, size - offset);
        if (count < 0) {
            fprintf(stderr, "[intl] ICU data not loaded: read error on %s.\n", path);
            free(buffer);
            close(fd);
            return;
        }
        if (count == 0) {
            break;
        }
        offset += (size_t) count;
    }
    close(fd);

    if (offset != size) {
        fprintf(stderr, "[intl] ICU data not loaded: short read on %s (%zu/%zu).\n",
                path, offset, size);
        free(buffer);
        return;
    }

    status = U_ZERO_ERROR;
    udata_setCommonData(buffer, &status);
    if (U_FAILURE(status)) {
        fprintf(stderr,
                "[intl] udata_setCommonData(%s) failed: %s. "
                "(Likely an ICU library/data version mismatch.)\n",
                path, u_errorName(status));
        free(buffer);
        return;
    }

    /* Validate the exact library/data pairing at module load, not first use. */
    status = U_ZERO_ERROR;
    u_init(&status);
    if (U_FAILURE(status)) {
        fprintf(stderr, "[intl] u_init after loading %s failed: %s.\n",
                path, u_errorName(status));
    }
}
