#define _POSIX_C_SOURCE 200809L

#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sp_vio.h>

typedef struct {
    void *vio;
    int src_width;
    int src_height;
    int dst_width;
    int dst_height;
    size_t input_size;
    size_t output_size;
    pthread_mutex_t lock;
} AIX5Vps;

void *aix5_vps_open(int src_width, int src_height, int dst_width, int dst_height) {
    if (src_width < 16 || src_height < 16 || dst_width < 16 || dst_height < 16 ||
        (src_width & 1) || (src_height & 1) || (dst_width & 1) || (dst_height & 1)) {
        return NULL;
    }
    AIX5Vps *vps = calloc(1, sizeof(*vps));
    if (!vps) return NULL;
    vps->vio = sp_init_vio_module();
    if (!vps->vio) { free(vps); return NULL; }
    vps->src_width = src_width;
    vps->src_height = src_height;
    vps->dst_width = dst_width;
    vps->dst_height = dst_height;
    vps->input_size = (size_t)src_width * src_height * 3 / 2;
    vps->output_size = (size_t)dst_width * dst_height * 3 / 2;
    int out_width = dst_width, out_height = dst_height;
    int ret = sp_open_vps(
        vps->vio, 0, 1, SP_VPS_SCALE,
        src_width, src_height, &out_width, &out_height,
        NULL, NULL, NULL, NULL, NULL
    );
    if (ret != 0 || out_width != dst_width || out_height != dst_height) {
        sp_release_vio_module(vps->vio);
        free(vps);
        return NULL;
    }
    pthread_mutex_init(&vps->lock, NULL);
    return vps;
}

int aix5_vps_scale(void *handle, const uint8_t *input, int input_size, uint8_t *output, int output_size, int timeout_ms) {
    AIX5Vps *vps = handle;
    if (!vps || !input || !output || input_size != (int)vps->input_size || output_size < (int)vps->output_size) return -1;
    pthread_mutex_lock(&vps->lock);
    int ret = sp_vio_set_frame(vps->vio, (void *)input, input_size);
    if (ret == 0) ret = sp_vio_get_frame(vps->vio, (char *)output, vps->dst_width, vps->dst_height, timeout_ms);
    pthread_mutex_unlock(&vps->lock);
    return ret;
}

void aix5_vps_close(void *handle) {
    AIX5Vps *vps = handle;
    if (!vps) return;
    pthread_mutex_lock(&vps->lock);
    sp_vio_close(vps->vio);
    pthread_mutex_unlock(&vps->lock);
    pthread_mutex_destroy(&vps->lock);
    sp_release_vio_module(vps->vio);
    free(vps);
}
