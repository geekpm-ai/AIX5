// RDK X5 hardware RTSP capture using hb_mm_mc_*.
// Output is a compact NV12 frame in a bounded ring in shared memory.
#define _POSIX_C_SOURCE 200809L
#include <libavformat/avformat.h>
#include <hb_media_codec.h>
#include <hb_media_error.h>
#include <signal.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdatomic.h>
#include <stdbool.h>

typedef struct {
    _Atomic uint64_t seq;
    uint32_t width, height;
    uint64_t timestamp_ns, frames;
    uint32_t codec, data_size;
    uint32_t active_slot;
    uint8_t reserved[20];
} Header;
_Static_assert(sizeof(Header)==64,"header size");
#define FRAME_SLOTS 8
static volatile sig_atomic_t stop_flag=0;
static uint64_t deadline=0;
static uint64_t now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000ULL+t.tv_nsec;}
static void on_signal(int s){(void)s;stop_flag=1;}
static int interrupt_io(void *x){(void)x;return stop_flag || (deadline && now_ns()>deadline);}
static int setup_decoder(media_codec_context_t *ctx,int codec,int w,int h){
    memset(ctx,0,sizeof(*ctx));ctx->encoder=0;ctx->codec_id=codec;
    mc_video_codec_dec_params_t *p=&ctx->video_dec_params;
    p->feed_mode=MC_FEEDING_MODE_FRAME_SIZE;p->pix_fmt=MC_PIXEL_FORMAT_NV12;
    p->bitstream_buf_size=2*1024*1024;p->bitstream_buf_count=3;p->frame_buf_count=6;
    if(codec==MEDIA_CODEC_ID_H264){p->h264_dec_config.bandwidth_Opt=true;p->h264_dec_config.reorder_enable=true;p->h264_dec_config.skip_mode=0;}
    else {p->h265_dec_config.bandwidth_Opt=true;p->h265_dec_config.reorder_enable=true;p->h265_dec_config.skip_mode=0;p->h265_dec_config.cra_as_bla=false;p->h265_dec_config.dec_temporal_id_mode=0;p->h265_dec_config.target_dec_temporal_id_plus1=0;}
    if(hb_mm_mc_initialize(ctx)!=0)return -1;if(hb_mm_mc_configure(ctx)!=0){hb_mm_mc_release(ctx);return -1;}
    mc_av_codec_startup_params_t startup={0};if(hb_mm_mc_start(ctx,&startup)!=0){hb_mm_mc_release(ctx);return -1;}return 0;
}
static int feed(media_codec_context_t *ctx,const uint8_t *data,int size){
    media_codec_buffer_t in={0};in.type=MC_VIDEO_STREAM_BUFFER;
    int r=hb_mm_mc_dequeue_input_buffer(ctx,&in,3000);if(r!=0)return r;
    if(in.vstream_buf.size < (uint32_t)size){hb_mm_mc_queue_input_buffer(ctx,&in,0);return -2;}
    memcpy(in.vstream_buf.vir_ptr,data,size);in.vstream_buf.size=size;in.vstream_buf.stream_end=0;
    return hb_mm_mc_queue_input_buffer(ctx,&in,3000);
}
static int receive_frame(media_codec_context_t *ctx,Header *out,size_t frame_size,uint8_t *dst){
    media_codec_buffer_t frame={0};media_codec_output_buffer_info_t info={0};
    int r=hb_mm_mc_dequeue_output_buffer(ctx,&frame,&info,3000);if(r!=0)return r;
    if(frame.type!=MC_VIDEO_FRAME_BUFFER || info.video_frame_info.decode_result==0 || frame.vframe_buf.size==0){hb_mm_mc_queue_output_buffer(ctx,&frame,0);return -3;}
    int w=frame.vframe_buf.width,h=frame.vframe_buf.height;int stride=frame.vframe_buf.stride;int vstride=frame.vframe_buf.vstride?frame.vframe_buf.vstride:stride;
    // The shared-memory slots were allocated for the negotiated stream size.
    // A resolution change must reconnect instead of writing beyond those slots.
    if(w!=(int)out->width||h!=(int)out->height||stride<w||vstride<w||
       (size_t)w*h*3/2!=frame_size||!frame.vframe_buf.vir_ptr[0]||!frame.vframe_buf.vir_ptr[1]){
        hb_mm_mc_queue_output_buffer(ctx,&frame,0);return -4;
    }
    uint32_t slot=(out->active_slot+1U)%FRAME_SLOTS;uint8_t *target=dst+(size_t)slot*frame_size;
    // Match the reader's seqlock protocol: odd means a slot is being written,
    // even means the complete frame is published and safe to copy.
    atomic_fetch_add_explicit(&out->seq,1,memory_order_seq_cst);
    for(int y=0;y<h;y++)memcpy(target+(size_t)y*w,frame.vframe_buf.vir_ptr[0]+(size_t)y*stride,w);
    for(int y=0;y<h/2;y++)memcpy(target+(size_t)w*h+(size_t)y*w,frame.vframe_buf.vir_ptr[1]+(size_t)y*vstride,w);
    ((uint64_t*)(dst+frame_size*FRAME_SLOTS))[slot]=now_ns();
    __sync_synchronize();out->width=w;out->height=h;out->data_size=frame_size;out->timestamp_ns=now_ns();out->frames++;out->active_slot=slot;atomic_fetch_add_explicit(&out->seq,1,memory_order_seq_cst);
    r=hb_mm_mc_queue_output_buffer(ctx,&frame,0);return r==0?0:r;
}
int main(int argc,char **argv){
    if(argc<2||!getenv("AIX5_RTSP_URL")){fprintf(stderr,"AIX5_RTSP_URL required\n");return 2;}
    signal(SIGINT,on_signal);signal(SIGTERM,on_signal);av_log_set_level(AV_LOG_QUIET);
    AVFormatContext *fmt=avformat_alloc_context();fmt->interrupt_callback=(AVIOInterruptCB){interrupt_io,NULL};AVDictionary *opts=NULL;
    av_dict_set(&opts,"rtsp_transport",getenv("AIX5_RTSP_TRANSPORT")?getenv("AIX5_RTSP_TRANSPORT"):"tcp",0);av_dict_set(&opts,"stimeout","5000000",0);
    deadline=now_ns()+15000000000ULL;
    int r=avformat_open_input(&fmt,getenv("AIX5_RTSP_URL"),NULL,&opts);av_dict_free(&opts);if(r<0)return 3;if(avformat_find_stream_info(fmt,NULL)<0)return 4;
    int stream=av_find_best_stream(fmt,AVMEDIA_TYPE_VIDEO,-1,-1,NULL,0);if(stream<0)return 5;AVCodecParameters *p=fmt->streams[stream]->codecpar;
    int codec=p->codec_id==AV_CODEC_ID_H264?MEDIA_CODEC_ID_H264:p->codec_id==AV_CODEC_ID_HEVC?MEDIA_CODEC_ID_H265:MEDIA_CODEC_ID_NONE;if(codec==MEDIA_CODEC_ID_NONE)return 6;
    int w=p->width,h=p->height;if(w<16||h<16||w>3840||h>2160||w%2||h%2)return 7;size_t frame_size=(size_t)w*h*3/2;size_t shm_size=64+frame_size*FRAME_SLOTS+FRAME_SLOTS*8;
    int fd=open(argv[1],O_RDWR|O_CREAT|O_TRUNC,0600);if(fd<0||ftruncate(fd,shm_size))return 8;Header *out=mmap(NULL,shm_size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);if(out==MAP_FAILED)return 9;memset(out,0,64);out->width=w;out->height=h;out->codec=codec;out->data_size=frame_size;uint32_t slots=FRAME_SLOTS;memcpy(out->reserved,&slots,4);
    media_codec_context_t ctx;if(setup_decoder(&ctx,codec,w,h)!=0){fprintf(stderr,"MM_DECODER_START_FAILED\n");return 10;}uint8_t *dst=(uint8_t*)out+64;AVPacket *pkt=av_packet_alloc();fprintf(stderr,"MM_HW_DECODER_READY width=%d height=%d codec=%d\n",w,h,codec);fflush(stderr);
    while(!stop_flag){deadline=now_ns()+7000000000ULL;r=av_read_frame(fmt,pkt);if(r<0)break;if(pkt->stream_index==stream&&pkt->size>0){if(feed(&ctx,pkt->data,pkt->size)==0){for(int i=0;i<3&&!stop_flag;i++){int gr=receive_frame(&ctx,out,frame_size,dst);if(gr==0)break;}}}av_packet_unref(pkt);}
    hb_mm_mc_pause(&ctx);hb_mm_mc_release(&ctx);av_packet_free(&pkt);avformat_close_input(&fmt);munmap(out,shm_size);return 0;
}
