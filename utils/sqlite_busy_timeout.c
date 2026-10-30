/* RTAB-Map aborts when sqlite3_step returns "database is locked".
 * Ask SQLite to wait and retry instead. Loaded with LD_PRELOAD. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <sqlite3.h>

static void arm(sqlite3 *db)
{
    if (db) {
        sqlite3_busy_timeout(db, 60000);
    }
}

int sqlite3_open(const char *filename, sqlite3 **ppDb)
{
    static int (*real_open)(const char *, sqlite3 **) = 0;
    int rc;

    if (!real_open) {
        real_open = dlsym(RTLD_NEXT, "sqlite3_open");
    }
    rc = real_open(filename, ppDb);
    if (rc == SQLITE_OK && ppDb) {
        arm(*ppDb);
    }
    return rc;
}

int sqlite3_open_v2(const char *filename, sqlite3 **ppDb, int flags, const char *zVfs)
{
    static int (*real_open_v2)(const char *, sqlite3 **, int, const char *) = 0;
    int rc;

    if (!real_open_v2) {
        real_open_v2 = dlsym(RTLD_NEXT, "sqlite3_open_v2");
    }
    rc = real_open_v2(filename, ppDb, flags, zVfs);
    if (rc == SQLITE_OK && ppDb) {
        arm(*ppDb);
    }
    return rc;
}
