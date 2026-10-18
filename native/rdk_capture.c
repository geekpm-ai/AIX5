// RTSP demux only (libavformat); video decoding exclusively uses RDK libspcdev.
#define _POSIX_C_SOURCE 200809L
#include <libavformat/avformat.h>
#include <sp_codec.h>
#include <pthread.h>
#include <stdatomic.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

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
static volatile sig_atomic_t stop=0;
static void *decoder;
static Header *shared;
static int width,height;
static uint64_t deadline=0;
static uint64_t now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;}
static void on_signal(int sig){(void)sig;stop=1;}
static int interrupt_io(void *x){(void)x;return stop || (deadline && now_ns()>deadline);}
int main(int argc,char **argv){
    if(argc<2 || !getenv("AIX5_RTSP_URL")){fprintf(stderr,"usage: rdk_capture SHM_PATH [CHANNEL] (AIX5_RTSP_URL required)\n");return 2;}
    signal(SIGINT,on_signal);signal(SIGTERM,on_signal);av_log_set_level(AV_LOG_QUIET);
    AVFormatContext *fmt=avformat_alloc_context();fmt->interrupt_callback=(AVIOInterruptCB){interrupt_io,NULL};
    AVDictionary *opts=NULL;av_dict_set(&opts,"rtsp_transport",getenv("AIX5_RTSP_TRANSPORT") ? getenv("AIX5_RTSP_TRANSPORT") : "tcp",0);av_dict_set(&opts,"stimeout","5000000",0);
    deadline=now_ns()+15000000000ULL;
    int r=avformat_open_input(&fmt,getenv("AIX5_RTSP_URL"),NULL,&opts);av_dict_free(&opts);
    if(r<0){fprintf(stderr,"RTSP_OPEN_FAILED code=%d\n",r);return 3;}
    if(avformat_find_stream_info(fmt,NULL)<0){fprintf(stderr,"STREAM_INFO_FAILED\n");avformat_close_input(&fmt);return 4;}
    int stream=av_find_best_stream(fmt,AVMEDIA_TYPE_VIDEO,-1,-1,NULL,0);
    if(stream<0){avformat_close_input(&fmt);return 5;}
    AVCodecParameters *p=fmt->streams[stream]->codecpar;
    int codec=p->codec_id==AV_CODEC_ID_H264?SP_ENCODER_H264:p->codec_id==AV_CODEC_ID_HEVC?SP_ENCODER_H265:0;
    width=p->width;height=p->height;
    if(!codec || width<16 || height<16 || width>3840 || height>2160 || width%2 || height%2){fprintf(stderr,"UNSUPPORTED_STREAM\n");avformat_close_input(&fmt);return 6;}
    size_t frame_size=(size_t)width*height*3/2;
    size_t size=64+frame_size*FRAME_SLOTS+FRAME_SLOTS*8;int fd=open(argv[1],O_RDWR|O_CREAT|O_TRUNC,0600);
    if(fd<0 || ftruncate(fd,size)){perror("shm");return 7;}
    shared=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);if(shared==MAP_FAILED)return 8;
    memset(shared,0,64);shared->width=width;shared->height=height;shared->codec=codec;shared->data_size=frame_size;shared->active_slot=0;uint32_t slots=FRAME_SLOTS;memcpy(shared->reserved,&slots,4);
    decoder=sp_init_decoder_module();int ch=argc>2?atoi(argv[2]):0;
    r=sp_start_decode(decoder,"",ch,codec,width,height);
    if(r){fprintf(stderr,"HW_DECODER_START_FAILED code=%d\n",r);return 9;}
    // RTSP packets from this camera carry VPS/SPS/PPS in-band. The vendor sample
    // builds a sequence header for file/MP4 input; do not pass AVCC/HVCC extradata
    // directly to the decoder because it is not Annex-B and can corrupt the VPU.
    fprintf(stderr,"HW_DECODER_READY width=%d height=%d codec=%d\n",width,height,codec);fflush(stderr);
    AVPacket *pkt=av_packet_alloc();
    void *buf=NULL; uint32_t write_slot=0;
    if (posix_memalign(&buf, 4096, (size_t)width*height*3/2 + 4096) != 0) { fprintf(stderr,"ALLOC_FAILED\n"); return 11; }
    while(!stop){
        deadline=now_ns()+7000000000ULL;r=av_read_frame(fmt,pkt);if(r<0)break;
        if(pkt->stream_index==stream && pkt->size>0) {
            int sr=sp_decoder_set_image(decoder,(char*)pkt->data,ch,pkt->size,0);
            if (sr != 0) { av_packet_unref(pkt); continue; }
            int gr=sp_decoder_get_image(decoder,buf);
            if (gr == 0) {
                size_t size=(size_t)width*height*3/2;
                atomic_fetch_add_explicit(&shared->seq,1,memory_order_seq_cst);
                memcpy((char *)shared+64+(size_t)write_slot*frame_size,buf,frame_size);
                shared->timestamp_ns=now_ns();shared->frames++;
                ((uint64_t*)((char*)shared+64+frame_size*FRAME_SLOTS))[write_slot]=shared->timestamp_ns;
                // Publish only after the complete slot is written. The reader
                // copies the other slot while this slot is being prepared.
                __sync_synchronize();
                shared->active_slot=write_slot;
                atomic_fetch_add_explicit(&shared->seq,1,memory_order_seq_cst);
                write_slot=(write_slot+1U)%FRAME_SLOTS;
            }
        }
        av_packet_unref(pkt);
    }
    free(buf);fprintf(stderr,"CAPTURE_STOP frames=%lu\n",shared->frames);
    sp_stop_decode(decoder);sp_release_decoder_module(decoder);av_packet_free(&pkt);avformat_close_input(&fmt);munmap(shared,size);
    return r<0?10:0;
}
