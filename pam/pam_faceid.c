/*
 * pam_faceid.c - Native Linux PAM Module for Face ID Authentication
 * Connects to /run/faceid/auth.sock (faceid-daemon) and returns PAM_SUCCESS
 * on neural face match or PAM_IGNORE to fall back cleanly to password auth.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#define PAM_SM_AUTH
#if __has_include(<security/pam_modules.h>)
#include <security/pam_appl.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>
#else
/* Standard Linux-PAM ABI definitions when pam-devel headers are not yet installed */
typedef struct pam_handle pam_handle_t;
#define PAM_EXTERN extern
#define PAM_SUCCESS 0
#define PAM_IGNORE 25
#define PAM_SERVICE 1
#define PAM_RHOST 4
extern int pam_get_user(pam_handle_t *pamh, const char **user, const char *prompt);
extern int pam_get_item(const pam_handle_t *pamh, int item_type, const void **item);
#endif

#define FACEID_SOCK_PRIMARY "/run/faceid/auth.sock"
#define FACEID_SOCK_FALLBACK "/tmp/faceid-runtime/auth.sock"
#define FACEID_DISABLE_FLAG "/etc/faceid/disabled"

static int connect_faceid_socket(int timeout_sec) {
    const char *paths[2] = {FACEID_SOCK_PRIMARY, FACEID_SOCK_FALLBACK};
    for (int i = 0; i < 2; i++) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) {
            continue;
        }

        struct timeval tv;
        tv.tv_sec = timeout_sec;
        tv.tv_usec = 0;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

        struct sockaddr_un addr;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        strncpy(addr.sun_path, paths[i], sizeof(addr.sun_path) - 1);

        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            return fd;
        }
        close(fd);
    }
    return -1;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags,
                                   int argc, const char **argv) {
    (void)flags;
    (void)argc;
    (void)argv;

    /* Emergency killswitch check */
    if (access(FACEID_DISABLE_FLAG, F_OK) == 0) {
        return PAM_IGNORE;
    }

    /* Skip remote sessions (e.g. SSH) */
    const void *rhost = NULL;
    if (pam_get_item(pamh, PAM_RHOST, &rhost) == PAM_SUCCESS && rhost != NULL) {
        const char *rhost_str = (const char *)rhost;
        if (rhost_str[0] != '\0' && strcmp(rhost_str, "localhost") != 0 &&
            strcmp(rhost_str, "127.0.0.1") != 0 && strcmp(rhost_str, "::1") != 0) {
            return PAM_IGNORE;
        }
    }

    const char *user = NULL;
    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL || user[0] == '\0') {
        return PAM_IGNORE;
    }

    const void *service = NULL;
    const char *service_str = "pam";
    if (pam_get_item(pamh, PAM_SERVICE, &service) == PAM_SUCCESS && service != NULL) {
        service_str = (const char *)service;
    }

    int fd = connect_faceid_socket(6);
    if (fd < 0) {
        return PAM_IGNORE;
    }

    char req[512];
    int n = snprintf(req, sizeof(req),
                     "{\"cmd\":\"AUTH\",\"user\":\"%s\",\"reason\":\"%s\"}\n",
                     user, service_str);
    if (n <= 0 || (size_t)n >= sizeof(req)) {
        close(fd);
        return PAM_IGNORE;
    }

    if (send(fd, req, (size_t)n, MSG_NOSIGNAL) < 0) {
        close(fd);
        return PAM_IGNORE;
    }

    char resp[1024];
    memset(resp, 0, sizeof(resp));
    ssize_t r = recv(fd, resp, sizeof(resp) - 1, 0);
    close(fd);

    if (r <= 0) {
        return PAM_IGNORE;
    }

    if (strstr(resp, "\"ok\": true") != NULL || strstr(resp, "\"ok\":true") != NULL) {
        return PAM_SUCCESS;
    }

    return PAM_IGNORE;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags,
                              int argc, const char **argv) {
    (void)pamh;
    (void)flags;
    (void)argc;
    (void)argv;
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_acct_mgmt(pam_handle_t *pamh, int flags,
                                int argc, const char **argv) {
    (void)pamh;
    (void)flags;
    (void)argc;
    (void)argv;
    return PAM_SUCCESS;
}
