#define _POSIX_C_SOURCE 200809L
// VPU encoder ABI and pipelined shared-memory stream. Input: compact NV12.
#include <hb_media_codec.h>
#include <hb_media_error.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <stdio.h>
#include <errno.h>

typedef struct {media_codec_context_t codec; int width,height,encoded_height;} Encoder;
void aix5_encoder_close(void *handle) {
    if(!handle)return; Encoder *e=handle;
    hb_mm_mc_pause(&e->codec);hb_mm_mc_release(&e->codec);free(e);
}
static void encoder_retry_sleep(int attempt) {
    /*
     * On a cold boot hb_mm_mc may be callable before the multimedia service
     * has finished bringing up the encoder hardware.  Keep the retry delay
     * short for the first attempts and cap it so a WebRTC offer is not held
     * indefinitely inside the native helper.
     */
    long delay_ms = 100L << (attempt < 4 ? attempt : 4);
    if (delay_ms > 1600L) delay_ms = 1600L;
    struct timespec delay = {delay_ms / 1000L, (delay_ms % 1000L) * 1000000L};
    nanosleep(&delay, NULL);
}

static void *aix5_encoder_open_once(int width,int height,int fps,int bitrate) {
    if(width<16||height<16||width%2||height%2)return NULL;
    Encoder *e=calloc(1,sizeof(*e));if(!e)return NULL;
    e->width=width;e->height=height;e->encoded_height=(height+7)&~7;
    media_codec_context_t *c=&e->codec;c->encoder=1;c->codec_id=MEDIA_CODEC_ID_H264;
    mc_video_codec_enc_params_t *p=&c->video_enc_params;
    p->width=width;p->height=e->encoded_height;p->pix_fmt=MC_PIXEL_FORMAT_NV12;
    p->frame_buf_count=3;p->external_frame_buf=0;p->bitstream_buf_count=3;
    p->bitstream_buf_size=(width*height*3/2+1023)&~1023;
    if(e->encoded_height!=height){p->frame_cropping_flag=1;p->crop_rect.x_pos=0;p->crop_rect.y_pos=0;p->crop_rect.width=width;p->crop_rect.height=height;}
    p->gop_params.gop_preset_idx=9;p->h264_enc_config.h264_profile=MC_H264_PROFILE_BP;p->enable_user_pts=1;
    p->rc_params.mode=MC_AV_RC_MODE_H264CBR;
    if(hb_mm_mc_get_rate_control_config(c,&p->rc_params)!=0){free(e);return NULL;}
    mc_h264_cbr_params_t *r=&p->rc_params.h264_cbr_params;
    r->frame_rate=fps;r->bit_rate=bitrate/1000;r->intra_period=fps;
    r->intra_qp=25;r->initial_rc_qp=25;r->vbv_buffer_size=3000;
    r->mb_level_rc_enalbe=1;r->min_qp_I=10;r->max_qp_I=45;
    r->min_qp_P=10;r->max_qp_P=45;r->min_qp_B=10;r->max_qp_B=45;
    r->hvs_qp_enable=1;r->hvs_qp_scale=2;r->max_delta_qp=10;
    if(hb_mm_mc_initialize(c)!=0){free(e);return NULL;}
    if(hb_mm_mc_configure(c)!=0){hb_mm_mc_release(c);free(e);return NULL;}
    mc_av_codec_startup_params_t start={0};
    if(hb_mm_mc_start(c,&start)!=0){hb_mm_mc_release(c);free(e);return NULL;}
    return e;
}
void *aix5_encoder_open(int width,int height,int fps,int bitrate) {
    /* Six attempts cover the usual 0.1+0.2+0.4+0.8+1.6s cold-boot race,
       while still returning promptly when the dimensions are unsupported. */
    for (int attempt = 0; attempt < 6; ++attempt) {
        void *handle = aix5_encoder_open_once(width, height, fps, bitrate);
        if (handle) return handle;
        if (attempt < 5) encoder_retry_sleep(attempt);
    }
    return NULL;
}
int aix5_encoder_submit(void *handle,const uint8_t *nv12,int bytes,int64_t pts,int keyframe) {
    Encoder *e=handle;if(!e||bytes!=e->width*e->height*3/2)return -1;
    media_codec_context_t *c=&e->codec;
    if(keyframe)hb_mm_mc_request_idr_frame(c);
    media_codec_buffer_t in={0};in.type=MC_VIDEO_FRAME_BUFFER;
    int result=hb_mm_mc_dequeue_input_buffer(c,&in,1000);
    if(result==HB_MEDIA_ERR_WAIT_TIMEOUT)return -2;
    if(result!=0){fprintf(stderr,"AIX5_ENCODER_ERROR stage=input_dequeue code=%d\n",result);return -7;}
    int stride=in.vframe_buf.stride?in.vframe_buf.stride:e->width;
    int uvstride=in.vframe_buf.vstride?in.vframe_buf.vstride:stride;
    if(!in.vframe_buf.vir_ptr[0]||!in.vframe_buf.vir_ptr[1]||stride<e->width||uvstride<e->width){hb_mm_mc_queue_input_buffer(c,&in,0);return -3;}
    for(int y=0;y<e->height;y++)memcpy(in.vframe_buf.vir_ptr[0]+y*stride,nv12+y*e->width,e->width);
    for(int y=0;y<e->height/2;y++)memcpy(in.vframe_buf.vir_ptr[1]+y*uvstride,nv12+e->width*e->height+y*e->width,e->width);
    for(int y=e->height;y<e->encoded_height;y++)memset(in.vframe_buf.vir_ptr[0]+y*stride,16,e->width);
    for(int y=e->height/2;y<e->encoded_height/2;y++)memset(in.vframe_buf.vir_ptr[1]+y*uvstride,128,e->width);
    in.vframe_buf.pts=pts;in.vframe_buf.size=e->width*e->encoded_height*3/2;
    result=hb_mm_mc_queue_input_buffer(c,&in,1000);
    if(result!=0)fprintf(stderr,"AIX5_ENCODER_ERROR stage=input_queue code=%d\n",result);
    return result==0?0:-4;
}
int aix5_encoder_receive(void *handle,uint8_t *output,int capacity,uint64_t *pts,int timeout) {
    Encoder *e=handle;if(!e)return -1;media_codec_context_t *c=&e->codec;
    media_codec_buffer_t out={0};media_codec_output_buffer_info_t info={0};
    int result=hb_mm_mc_dequeue_output_buffer(c,&out,&info,timeout);
    if(result==HB_MEDIA_ERR_WAIT_TIMEOUT)return -5;
    if(result!=0){fprintf(stderr,"AIX5_ENCODER_ERROR stage=output_dequeue code=%d\n",result);return -8;}
    int size=out.vstream_buf.size;if(pts)*pts=out.vstream_buf.pts;
    if(size>0&&size<=capacity)memcpy(output,out.vstream_buf.vir_ptr,size);
    result=hb_mm_mc_queue_output_buffer(c,&out,0);
    if(result!=0){fprintf(stderr,"AIX5_ENCODER_ERROR stage=output_release code=%d\n",result);return -9;}
    return size<=capacity?size:-6;
}

int aix5_encoder_encode(void *handle,const uint8_t *nv12,int bytes,int64_t pts,int keyframe,uint8_t *output,int capacity) {
    int result=aix5_encoder_submit(handle,nv12,bytes,pts,keyframe);
    if(result!=0)return result;
    return aix5_encoder_receive(handle,output,capacity,NULL,1000);
}

#ifdef AIX5_STREAM_MAIN
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <stdio.h>
#include <time.h>
#include <stdatomic.h>
#include <pthread.h>
#include <poll.h>
#include <libswscale/swscale.h>
#include <libavutil/pixfmt.h>

typedef struct {
    _Atomic uint64_t seq;uint32_t width,height;uint64_t timestamp_ns,frames;
    uint32_t codec,data_size,active_slot;uint8_t reserved[20];
} CaptureHeader;
/* Lock-free atomics are safe in both the signal handler and worker thread. */
_Static_assert(ATOMIC_INT_LOCK_FREE==2,"stream stop must be signal-safe");
static _Atomic int stream_stop=0;
static _Atomic int stream_exit_code=0;
static _Atomic uint64_t stream_output_frames=0;
static _Atomic uint64_t stream_received_frames=0;
static _Atomic uint64_t stream_output_timeouts=0;
static _Atomic uint64_t stream_pipe_waits=0;
/* 1: waiting for VPU, 2: writing header, 3: writing payload. */
static _Atomic int stream_output_stage=0;
static const char *output_stage_name(int stage){
    return stage==1?"vpu_receive":stage==2?"pipe_header":stage==3?"pipe_payload":"idle";
}
static void stop_stream(int signal_number){(void)signal_number;stream_stop=1;}
static int write_all(int fd,const void *data,size_t size){
    const uint8_t *p=data;
    while(size&&!stream_stop){
        ssize_t n=write(fd,p,size);
        if(n<0&&errno==EINTR)continue;
        if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK)){
            atomic_fetch_add(&stream_pipe_waits,1);
            /* A SIGTERM may be delivered to the input thread. Poll in short
               intervals so a blocked writer still observes the stop flag. */
            struct pollfd ready={fd,POLLOUT,0};
            if(poll(&ready,1,100)<0&&errno!=EINTR)return -1;
            continue;
        }
        if(n<=0)return -1;
        p+=n;size-=n;
    }
    return size?-1:0;
}
typedef struct {void *encoder;int pipe_fd,width,height;} OutputContext;
static void *stream_output(void *opaque){
    OutputContext *context=opaque;int capacity=context->width*((context->height+7)&~7)*3/2+65536;uint8_t *encoded=malloc(capacity);
    if(!encoded){fprintf(stderr,"AIX5_STREAM_ERROR stage=output_alloc\n");atomic_store(&stream_exit_code,21);stream_stop=1;return NULL;}
    while(!stream_stop){
        atomic_store(&stream_output_stage,1);
        uint64_t pts=0;int count=aix5_encoder_receive(context->encoder,encoded,capacity,&pts,100);
        if(count==-5){atomic_fetch_add(&stream_output_timeouts,1);continue;} // no completed frame yet
        if(count<=0){fprintf(stderr,"AIX5_STREAM_ERROR stage=receive code=%d\n",count);atomic_store(&stream_exit_code,22);stream_stop=1;break;}
        atomic_fetch_add(&stream_received_frames,1);
        uint32_t header[5];memcpy(header,&pts,8);header[2]=context->width;header[3]=context->height;header[4]=count;
        atomic_store(&stream_output_stage,2);
        int written=write_all(context->pipe_fd,header,20);
        if(!written){atomic_store(&stream_output_stage,3);written=write_all(context->pipe_fd,encoded,count);}
        if(written){if(!stream_stop){fprintf(stderr,"AIX5_STREAM_ERROR stage=pipe_write errno=%d\n",errno);atomic_store(&stream_exit_code,23);}stream_stop=1;break;}
        atomic_fetch_add(&stream_output_frames,1);
    }
    free(encoded);return NULL;
}
int main(int argc,char **argv){
    if(argc!=5)return 2;
    int fd=atoi(argv[1]),target_width=atoi(argv[2]),target_height=atoi(argv[3]),fps=atoi(argv[4]);
    if(target_width<16||target_height<16||fps<1||fps>120)return 2;
    int pipe_fd=dup(STDOUT_FILENO);
    if(pipe_fd<0||fcntl(pipe_fd,F_SETFL,fcntl(pipe_fd,F_GETFL)|O_NONBLOCK)<0)return 3;
    dup2(STDERR_FILENO,STDOUT_FILENO);setvbuf(stderr,NULL,_IONBF,0);
    signal(SIGTERM,stop_stream);signal(SIGINT,stop_stream);signal(SIGPIPE,stop_stream);
    struct stat statbuf;if(fstat(fd,&statbuf)||statbuf.st_size<64)return 3;
    CaptureHeader *shared=mmap(NULL,statbuf.st_size,PROT_READ,MAP_SHARED,fd,0);if(shared==MAP_FAILED)return 4;
    int width=shared->width,height=shared->height;uint32_t slots=0;memcpy(&slots,shared->reserved,4);int ring=slots>=2&&slots<=8;
    if(!ring)slots=2;
    if(width<16||height<16||width%2||height%2||width>3840||height>2160)return 5;
    if(target_width>width)target_width=width;if(target_height>height)target_height=height;
    // Fit without aspect distortion or upscaling.
    double scale=(double)target_width/width;if((double)target_height/height<scale)scale=(double)target_height/height;
    target_width=((int)(width*scale))&~1;target_height=((int)(height*scale))&~1;
    int frame_size=width*height*3/2,out_size=target_width*target_height*3/2;
    uint8_t *input=malloc(frame_size),*scaled=malloc(out_size);
    if(!input||!scaled)return 6;
    struct SwsContext *scaler=NULL;
    if(width!=target_width||height!=target_height)scaler=sws_getContext(width,height,AV_PIX_FMT_NV12,target_width,target_height,AV_PIX_FMT_NV12,SWS_FAST_BILINEAR,NULL,NULL,NULL);
    if((width!=target_width||height!=target_height)&&!scaler)return 6;
    void *encoder=aix5_encoder_open(target_width,target_height,fps,target_width>=1920?4000000:1500000);if(!encoder){fprintf(stderr,"AIX5_STREAM_ERROR stage=encoder_open width=%d height=%d fps=%d\n",target_width,target_height,fps);return 7;}
    OutputContext output_context={encoder,pipe_fd,target_width,target_height};pthread_t output_thread;
    if(pthread_create(&output_thread,NULL,stream_output,&output_context)!=0)return 8;
    uint64_t last_seq=0,origin=0,frame_index=0;unsigned input_timeouts=0,consecutive_timeouts=0;
    int force_keyframe=1;
    fprintf(stderr,"AIX5_STREAM_READY width=%d height=%d fps=%d\n",target_width,target_height,fps);
    struct timespec pause={0,1000000};
    while(!stream_stop){
        uint64_t seq=atomic_load_explicit(&shared->seq,memory_order_acquire);
        if(!seq||(seq&1)||seq==last_seq){nanosleep(&pause,NULL);continue;}
        uint32_t size=shared->data_size,slot=shared->active_slot;uint64_t timestamp=shared->timestamp_ns;
        atomic_thread_fence(memory_order_acquire);
        if(atomic_load_explicit(&shared->seq,memory_order_acquire)!=seq)continue;
        // Catch up within a small bounded ring instead of losing frames during
        // a VPU queue wait. Old two-slot capture ABI remains supported.
        uint64_t target_seq=seq;
        if(ring&&last_seq&&seq>last_seq&&seq-last_seq<(uint64_t)slots*2)target_seq=last_seq+2;
        if(slot>=slots){fprintf(stderr,"AIX5_STREAM_ERROR stage=shm_slot slot=%u slots=%u\n",slot,slots);atomic_store(&stream_exit_code,24);break;}
        slot=(slot+slots-(seq-target_seq)/2)%slots;
        if(size!=(uint32_t)frame_size||64+(size_t)size*slots+(ring?slots*8:0)>(size_t)statbuf.st_size||shared->width!=(uint32_t)width||shared->height!=(uint32_t)height){fprintf(stderr,"AIX5_STREAM_ERROR stage=shm_dimensions width=%u height=%u size=%u\n",shared->width,shared->height,size);atomic_store(&stream_exit_code,25);break;}
        if(ring)timestamp=((const uint64_t*)((const uint8_t*)shared+64+(size_t)size*slots))[slot];
        memcpy(input,(const uint8_t*)shared+64+(size_t)slot*size,size);
        atomic_thread_fence(memory_order_acquire);
        uint64_t after=atomic_load_explicit(&shared->seq,memory_order_acquire);
        // A publication into the other slot does not invalidate this copy.
        if(after<seq||after-target_seq>=(uint64_t)slots*2-1)continue;
        last_seq=target_seq;if(!origin)origin=timestamp;
        uint8_t *nv12=input;
        if(scaler){const uint8_t *src[]={input,input+width*height,NULL,NULL};int src_stride[]={width,width,0,0};uint8_t *dst[]={scaled,scaled+target_width*target_height,NULL,NULL};int dst_stride[]={target_width,target_width,0,0};sws_scale(scaler,src,src_stride,0,height,dst,dst_stride);nv12=scaled;}
        uint64_t pts=(timestamp-origin)*9/100000;
        int result=aix5_encoder_submit(encoder,nv12,out_size,pts,force_keyframe||frame_index%fps==0);
        if(stream_stop)break;
        if(result==-2&&!stream_stop){
            /* A slow stdout reader may briefly fill the VPU output buffers.
               Drop this unsubmitted input and catch up at the live edge; a
               queue timeout does not invalidate the running codec context. */
            input_timeouts++;consecutive_timeouts++;force_keyframe=1;
            if(consecutive_timeouts==1||consecutive_timeouts%5==0)fprintf(stderr,"AIX5_STREAM_BACKPRESSURE input_timeouts=%u consecutive=%u output_frames=%llu received_frames=%llu output_stage=%s output_timeouts=%llu pipe_waits=%llu\n",input_timeouts,consecutive_timeouts,(unsigned long long)atomic_load(&stream_output_frames),(unsigned long long)atomic_load(&stream_received_frames),output_stage_name(atomic_load(&stream_output_stage)),(unsigned long long)atomic_load(&stream_output_timeouts),(unsigned long long)atomic_load(&stream_pipe_waits));
            if(consecutive_timeouts<10)continue;
        }
        if(result!=0){fprintf(stderr,"AIX5_STREAM_ERROR stage=submit code=%d frames=%llu\n",result,(unsigned long long)frame_index);atomic_store(&stream_exit_code,26);break;}
        frame_index++;consecutive_timeouts=0;force_keyframe=0;
    }
    stream_stop=1;pthread_join(output_thread,NULL);
    aix5_encoder_close(encoder);if(scaler)sws_freeContext(scaler);free(input);free(scaled);munmap(shared,statbuf.st_size);close(pipe_fd);
    int exit_code=atomic_load(&stream_exit_code);
    fprintf(stderr,"AIX5_STREAM_STOP code=%d input_frames=%llu output_frames=%llu input_timeouts=%u\n",exit_code,(unsigned long long)frame_index,(unsigned long long)atomic_load(&stream_output_frames),input_timeouts);
    return exit_code;
}
#endif
