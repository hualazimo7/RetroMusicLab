/* refdump.c — 2SF 的「黄金参照」导出器（**只在开发机用，不进发布包**）
 *
 * 这份代码是 kode54 `audacious-xsf/plugin.cc` 里 2SF 路径的**逐行移植**：
 *
 *   twosf_loader_state / load_twosf_map / load_twosf_mapz / twosf_loader /
 *   twosf_info      ← 原样，只把 C++ 构造/析构换成 memset + 显式 free
 *   xsf_open() 里 `psf_version == 0x24` 那一支   ← 原样
 *   xsf_render() 里 `case 0x24`                   ← 原样
 *   play() 里的时长/淡出算法                      ← 原样
 *
 * 唯一改掉的是 audacious 的 `write_audio()`，换成写裸 PCM
 * （s16le / 44100 / 立体声交错）。
 *
 * 为什么需要它
 * ------------
 * 判据必须是「逐样本最大差 == 0」。如果参照物就是我自己写的 `vio2wav.c`，
 * 那只是拿自己验自己。这里拿的是**上游作者的原始实现**当尺子 —— 容器解析、
 * 映射表拼接、帧数换算、淡出斜坡，只要我抄错一处，样本就会差。
 * （上一轮 `.hwas` 就是把判据定错才把 DC 漂移误判成「做不到」。）
 *
 * 用法
 * ----
 *   refdump <input.mini2sf> <output.pcm>
 *
 * 退出码同 vio2wav。
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#include "state.h"

#include <psflib.h>

#define OUT_RATE    44100
#define OUT_CH      2
#define MIN_BUFFER_SIZE 576     /* ⚠ 必须和 plugin.cc 一致（plugin.cc:56） */

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4
};

static uint32_t get_le32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8)
         | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

/* ================================================================== */
/*  以下到「主流程」为止，全部照 plugin.cc 搬                           */
/* ================================================================== */

struct twosf_loader_state {
    uint8_t *rom;
    uint8_t *state;
    size_t rom_size;
    size_t state_size;

    int initial_frames;
    int sync_type;
    int clockdown;
    int arm9_clockdown_level;
    int arm7_clockdown_level;
};

static int
load_twosf_map(struct twosf_loader_state *state, int issave, const unsigned char *udata, unsigned usize) {
    if (usize < 8) return -1;

    unsigned char *iptr;
    size_t isize;
    unsigned char *xptr;
    unsigned xsize = get_le32(udata + 4);
    unsigned xofs = get_le32(udata + 0);
    if (issave) {
        iptr = state->state;
        isize = state->state_size;
        state->state = 0;
        state->state_size = 0;
    }
    else {
        iptr = state->rom;
        isize = state->rom_size;
        state->rom = 0;
        state->rom_size = 0;
    }
    if (!iptr) {
        size_t rsize = xofs + xsize;
        if (!issave) {
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        iptr = (unsigned char *) malloc(rsize + 10);
        if (!iptr)
            return -1;
        memset(iptr, 0, rsize + 10);
        isize = rsize;
    }
    else if (isize < xofs + xsize) {
        size_t rsize = xofs + xsize;
        if (!issave) {
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        xptr = (unsigned char *) realloc(iptr, xofs + rsize + 10);
        if (!xptr) {
            free(iptr);
            return -1;
        }
        iptr = xptr;
        isize = rsize;
    }
    memcpy(iptr + xofs, udata + 8, xsize);
    if (issave) {
        state->state = iptr;
        state->state_size = isize;
    }
    else {
        state->rom = iptr;
        state->rom_size = isize;
    }
    return 0;
}

static int
load_twosf_mapz(struct twosf_loader_state *state, int issave, const unsigned char *zdata,
                unsigned zsize, unsigned zcrc) {
    int ret;
    int zerr;
    uLongf usize = 8;
    uLongf rsize = usize;
    unsigned char *udata;
    unsigned char *rdata;

    udata = (unsigned char *) malloc(usize);
    if (!udata)
        return -1;

    while (Z_OK != (zerr = uncompress(udata, &usize, zdata, zsize))) {
        if (Z_MEM_ERROR != zerr && Z_BUF_ERROR != zerr) {
            free(udata);
            return -1;
        }
        if (usize >= 8) {
            usize = get_le32(udata + 4) + 8;
            if (usize < rsize) {
                rsize += rsize;
                usize = rsize;
            }
            else
                rsize = usize;
        }
        else {
            rsize += rsize;
            usize = rsize;
        }
        rdata = (unsigned char *) realloc(udata, usize);
        if (!rdata) {
            free(udata);
            return -1;
        }
        udata = rdata;
    }

    rdata = (unsigned char *) realloc(udata, usize);
    if (!rdata) {
        free(udata);
        return -1;
    }

    if (0) {
        uLong ccrc = crc32(crc32(0L, Z_NULL, 0), rdata, (uInt) usize);
        if (ccrc != zcrc)
            return -1;
    }

    ret = load_twosf_map(state, issave, rdata, (unsigned) usize);
    free(rdata);
    return ret;
}

static int
twosf_loader(void * context, const uint8_t * exe, size_t exe_size,
             const uint8_t * reserved, size_t reserved_size) {
    struct twosf_loader_state * state = ( struct twosf_loader_state * ) context;

    if ( exe_size >= 8 ) {
        if ( load_twosf_map(state, 0, exe, (unsigned) exe_size) )
            return -1;
    }

    if ( reserved_size ) {
        size_t resv_pos = 0;
        if ( reserved_size < 16 )
            return -1;
        while ( resv_pos + 12 < reserved_size ) {
            unsigned save_size = get_le32(reserved + resv_pos + 4);
            unsigned save_crc = get_le32(reserved + resv_pos + 8);
            if (get_le32(reserved + resv_pos + 0) == 0x45564153) {
                if (resv_pos + 12 + save_size > reserved_size)
                    return -1;
                if (load_twosf_mapz(state, 1, reserved + resv_pos + 12, save_size, save_crc))
                    return -1;
            }
            resv_pos += 12 + save_size;
        }
    }

    return 0;
}

static int
twosf_info(void * context, const char * name, const char * value) {
    struct twosf_loader_state * state = ( struct twosf_loader_state * ) context;
    char *end;

    if ( !strcasecmp( name, "_frames" ) ) {
        state->initial_frames = (int)strtol( value, &end, 10 );
    }
    else if ( !strcasecmp( name, "_clockdown" ) ) {
        state->clockdown = (int)strtol( value, &end, 10 );
    }
    else if ( !strcasecmp( name, "_vio2sf_sync_type") ) {
        state->sync_type = (int)strtol( value, &end, 10 );
    }
    else if ( !strcasecmp( name, "_vio2sf_arm9_clockdown_level" ) ) {
        state->arm9_clockdown_level = (int)strtol( value, &end, 10 );
    }
    else if ( !strcasecmp( name, "_vio2sf_arm7_clockdown_level" ) ) {
        state->arm7_clockdown_level = (int)strtol( value, &end, 10 );
    }

    return 0;
}

/* ================================================================== */
/*  以下是 harness 部分（替代 audacious 的音频输出与 settings）         */
/* ================================================================== */

/*: psflib 的 `psf_info_meta` 会把 length/fade 解析成毫秒存进这里。
 *  plugin.cc 用的是全局 `info_state`，这里照抄成全局。 */
typedef struct {
    int tag_song_ms;
    int tag_fade_ms;
} info_state_t;

static info_state_t g_info;

/*: plugin.cc 的 `psf_info_meta` 里对 length / fade 的处理（第 320~335 行）。
 *  这里只关心这两个，其余标签（title/game/artist）参照用不到。 */
static int meta_cb(void *ctx, const char *name, const char *value)
{
    (void)ctx;
    if (!name || !value) return 0;
    if (!strcasecmp(name, "length") || !strcasecmp(name, "fade")) {
        /* 原实现用 `parse_time_crap`，它对非法值返回 BORK_TIME。 */
        extern long ref_parse_time_crap(const char *input);
        long n = ref_parse_time_crap(value);
        if (n != -1) {
            if (!strcasecmp(name, "length")) g_info.tag_song_ms = (int)n;
            else                             g_info.tag_fade_ms = (int)n;
        }
    }
    return 0;
}

/* ---- 文件回调 ---- */
static void *f_open(void *ctx, const char *path) { (void)ctx; return (void *)fopen(path, "rb"); }
static size_t f_read(void *buf, size_t size, size_t count, void *h) { return fread(buf, size, count, (FILE *)h); }
static int f_seek(void *h, int64_t ofs, int whence) { return fseek((FILE *)h, (long)ofs, whence); }
static int f_close(void *h) { return fclose((FILE *)h); }
static long f_tell(void *h) { return ftell((FILE *)h); }

static const psf_file_callbacks g_callbacks = {
    "\\/", NULL, f_open, f_read, f_seek, f_close, f_tell
};

/*: 照 plugin.cc 的 `parse_time_crap` 抄，但非法值返回 -1（代替 BORK_TIME）。 */
long ref_parse_time_crap(const char *input)
{
    int value = 0;
    int multiplier = 1000;
    const char *ptr = input;
    char *errordef;
    int minutes, seconds, hundredths;

    if (!input || !*input) return -1;

    minutes = strtol(ptr, &errordef, 10);
    if (errordef == ptr) return -1;
    if (*errordef == ':') {
        ptr = errordef + 1;
        seconds = strtol(ptr, &errordef, 10);
        if (errordef == ptr) return -1;
        if (*errordef == '.' || *errordef == ',') {
            ptr = errordef + 1;
            hundredths = strtol(ptr, &errordef, 10);
            if (errordef == ptr) return -1;
            value = ((minutes * 60) + seconds) * 1000 + hundredths * 10;
        } else {
            value = ((minutes * 60) + seconds) * 1000;
        }
    } else if (*errordef == '.' || *errordef == ',') {
        /* 纯秒数：83.5 */
        double d = strtod(input, &errordef);
        if (errordef == input) return -1;
        value = (int)(d * 1000.0);
    } else {
        value = minutes * multiplier;
    }
    return value;
}

int main(int argc, char **argv)
{
    const char *in_path, *out_path;
    struct twosf_loader_state state;
    NDS_state *nds_state;
    FILE *out;
    int rval;
    short buffer[MIN_BUFFER_SIZE * 2];
    int sample_rate = OUT_RATE;
    int max_buffer_samples = MIN_BUFFER_SIZE;
    int stream_samples_amount, fade_samples;
    int current_sample_pos = 0;
    long written = 0;

    if (argc < 3) {
        fprintf(stderr, "用法: refdump <input.mini2sf> <output.pcm>\n");
        return EXIT_USAGE;
    }
    in_path = argv[1];
    out_path = argv[2];

    memset(&g_info, 0, sizeof(g_info));
    memset(&state, 0, sizeof(state));
    state.initial_frames = -1;

    /* ---- xsf_open(): 先读 meta 拿 length/fade ---- */
    rval = psf_load(in_path, &g_callbacks, 0, 0, 0, meta_cb, &g_info, 0, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr, "ERR 读 meta 失败 (%d)\n", rval);
        return EXIT_PARSE;
    }

    /* ---- xsf_open(): psf_version == 0x24 那一支 ---- */
    nds_state = (NDS_state *) calloc(1, sizeof(*nds_state));
    if (!nds_state) return EXIT_NOMEM;
    if (state_init(nds_state)) return EXIT_NOMEM;

    if (psf_load(in_path, &g_callbacks, 0x24, twosf_loader, &state,
                 twosf_info, &state, 1, NULL, NULL) <= 0) {
        fprintf(stderr, "ERR psf_load 失败\n");
        return EXIT_PARSE;
    }

    if (!state.arm7_clockdown_level) state.arm7_clockdown_level = state.clockdown;
    if (!state.arm9_clockdown_level) state.arm9_clockdown_level = state.clockdown;

    nds_state->dwInterpolation = 1;
    nds_state->dwChannelMute = 0;

    nds_state->initial_frames = state.initial_frames;
    nds_state->sync_type = state.sync_type;
    nds_state->arm7_clockdown_level = state.arm7_clockdown_level;
    nds_state->arm9_clockdown_level = state.arm9_clockdown_level;

    if (state.rom)
        state_setrom(nds_state, state.rom, (u32)state.rom_size, 0);

    state_loadstate(nds_state, state.state, (u32)state.state_size);

    /* ---- play() 的时长/淡出算法，原样 ---- */
    /* 没有 length 标签时的兜底：plugin.cc 用 settings.fade_delay(170) +
       settings.fade_length(默认 10)，即 180 秒。这里写死同一数值。 */
    {
        long long total_ms = g_info.tag_song_ms
            ? (long long)(g_info.tag_song_ms + g_info.tag_fade_ms)
            : (long long)(170.0 + 10.0) * 1000;
        long long fade_ms = g_info.tag_song_ms
            ? (long long)g_info.tag_fade_ms
            : (long long)(10.0 * 1000);
        stream_samples_amount = (int)(total_ms * sample_rate / 1000LL);
        fade_samples = (int)(fade_ms * sample_rate / 1000LL);
    }

    out = fopen(out_path, "wb");
    if (!out) { fprintf(stderr, "ERR 无法写入 %s\n", out_path); return EXIT_USAGE; }

    /* ---- play() 主循环，原样（去掉 check_stop / check_seek） ---- */
    while (1) {
        int toget = max_buffer_samples;

        if (current_sample_pos >= stream_samples_amount)
            break;
        if (current_sample_pos + toget > stream_samples_amount)
            toget = stream_samples_amount - current_sample_pos;

        /* xsf_render(): case 0x24 */
        state_render(nds_state, buffer, (unsigned int)toget);

        if (fade_samples > 0) {
            int samples_into_fade =
                    current_sample_pos - (stream_samples_amount - fade_samples);
            if (samples_into_fade + toget > 0) {
                for (int j = 0; j < toget; j++, samples_into_fade++) {
                    if (samples_into_fade > 0) {
                        double fadedness =
                                (double)(fade_samples - samples_into_fade) / fade_samples;
                        for (int k = 0; k < 2; k++)
                            buffer[j * 2 + k] *= fadedness;
                    }
                }
            }
        }

        fwrite(buffer, sizeof(short) * OUT_CH, (size_t)toget, out);
        written += toget;
        current_sample_pos += toget;
    }

    fclose(out);

    if (written == 0) {
        fprintf(stderr, "ERR 渲染出 0 个样本\n");
        return EXIT_EMPTY;
    }

    if (getenv("REFDUMP_VERBOSE"))
        fprintf(stderr, "OK %ld 帧 %.3f 秒\n",
                written, (double)written / sample_rate);

    return EXIT_OK;
}
