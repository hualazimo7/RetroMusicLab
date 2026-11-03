/* refdump.c — SSF/DSF 的「黄金参照」导出器（**只在开发机用，不进发布包**）
 *
 * 这份代码是 kode54 `audacious-xsf/plugin.cc` 里 SSF/DSF 路径的**逐行移植**：
 *
 *   sdsf_loader                                  ← 原样
 *   xsf_open() 里 `psf_version == 0x11 || 0x12` 那一支  ← 原样
 *   xsf_render() 里 `case 0x11: case 0x12:`      ← 原样
 *   play() 里的时长/淡出算法                      ← 原样
 *
 * 唯一改掉的是 audacious 的 `write_audio()`，换成写裸 PCM
 * （s16le / 44100 / 立体声交错）。
 *
 * 用法
 * ----
 *   refdump <input.ssf|.dsf> <output.pcm>
 *
 * 退出码同 ssf2wav。
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sega.h"

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

static void set_le32(void *p, uint32_t v)
{
    uint8_t *b = (uint8_t *)p;
    b[0] = (uint8_t)(v & 0xFF);
    b[1] = (uint8_t)((v >> 8) & 0xFF);
    b[2] = (uint8_t)((v >> 16) & 0xFF);
    b[3] = (uint8_t)((v >> 24) & 0xFF);
}

/* ================================================================== */
/*  以下照 plugin.cc 搬                                                */
/* ================================================================== */

struct sdsf_loader_state {
    uint8_t * data;
    size_t data_size;
};

static int
sdsf_loader(void * context, const uint8_t * exe, size_t exe_size,
            const uint8_t * reserved, size_t reserved_size) {
    (void)reserved;
    (void)reserved_size;

    if ( exe_size < 4 ) return -1;

    struct sdsf_loader_state * state = ( struct sdsf_loader_state * ) context;

    uint8_t * dst = state->data;

    if ( state->data_size < 4 ) {
        state->data = dst = ( uint8_t * ) malloc( exe_size );
        state->data_size = exe_size;
        memcpy( dst, exe, exe_size );
        return 0;
    }

    uint32_t dst_start = get_le32( dst );
    uint32_t src_start = get_le32( exe );
    dst_start &= 0x7fffff;
    src_start &= 0x7fffff;
    size_t dst_len = state->data_size - 4;
    size_t src_len = exe_size - 4;
    if ( dst_len > 0x800000 ) dst_len = 0x800000;
    if ( src_len > 0x800000 ) src_len = 0x800000;

    if ( src_start < dst_start ) {
        uint32_t diff = dst_start - src_start;
        state->data_size = dst_len + 4 + diff;
        state->data = dst = ( uint8_t * ) realloc( dst, state->data_size );
        memmove( dst + 4 + diff, dst + 4, dst_len );
        memset( dst + 4, 0, diff );
        dst_len += diff;
        dst_start = src_start;
        set_le32( dst, dst_start );
    }
    if ( ( src_start + src_len ) > ( dst_start + dst_len ) ) {
        size_t diff = ( src_start + src_len ) - ( dst_start + dst_len );
        state->data_size = dst_len + 4 + diff;
        state->data = dst = ( uint8_t * ) realloc( dst, state->data_size );
        memset( dst + 4 + dst_len, 0, diff );
    }

    memcpy( dst + 4 + ( src_start - dst_start ), exe + 4, src_len );

    return 0;
}

/* ================================================================== */
/*  harness 部分                                                       */
/* ================================================================== */

typedef struct {
    int tag_song_ms;
    int tag_fade_ms;
} info_state_t;

static info_state_t g_info;

/*: plugin.cc 的 `parse_time_crap`（第 275~319 行）**逐行照抄**。
 *
 * ⚠ 第一版我凭印象写了一个「分:秒.百分秒」的解析器，把 `1:18.6` 解成
 *   78060 毫秒（18 秒 + 6 百分秒）。上游其实是用 `strtod` 读最后一节，
 *   得到 **78600**（18.6 秒）。差 540 毫秒 = 23814 帧，三个文件全部
 *   「字节数不同」—— 正是「参照物必须照抄、不能凭印象」的活例子。 */
long ref_parse_time_crap(const char *input)
{
    unsigned long value = 0;
    unsigned long multiplier = 1000;
    const char *ptr = input;
    unsigned long colon_count = 0;

    while (*ptr && ((*ptr >= '0' && *ptr <= '9') || *ptr == ':')) {
        colon_count += *ptr == ':';
        ++ptr;
    }
    if (colon_count > 2) return -1;
    if (*ptr && *ptr != '.' && *ptr != ',') return -1;
    if (*ptr) ++ptr;
    while (*ptr && *ptr >= '0' && *ptr <= '9') ++ptr;
    if (*ptr) return -1;

    ptr = strrchr(input, ':');
    if (!ptr)
        ptr = input;
    for (;;) {
        char *end;
        if (ptr != input) ++ptr;
        if (multiplier == 1000) {
            double temp = strtod(ptr, &end);
            if (temp >= 60.0) return -1;
            value = (long)(temp * 1000.0f);
        } else {
            unsigned long temp = strtoul(ptr, &end, 10);
            if (temp >= 60 && multiplier < 3600000) return -1;
            value += temp * multiplier;
        }
        if (ptr == input) break;
        ptr -= 2;
        while (ptr > input && *ptr != ':') --ptr;
        multiplier *= 60;
    }

    return (long)value;
}

static int meta_cb(void *ctx, const char *name, const char *value)
{
    (void)ctx;
    if (!name || !value) return 0;
    if (!strcasecmp(name, "length") || !strcasecmp(name, "fade")) {
        long n = ref_parse_time_crap(value);
        if (n != -1) {
            if (!strcasecmp(name, "length")) g_info.tag_song_ms = (int)n;
            else                             g_info.tag_fade_ms = (int)n;
        }
    }
    return 0;
}

static void *f_open(void *ctx, const char *path) { (void)ctx; return (void *)fopen(path, "rb"); }
static size_t f_read(void *buf, size_t size, size_t count, void *h) { return fread(buf, size, count, (FILE *)h); }
static int f_seek(void *h, int64_t ofs, int whence) { return fseek((FILE *)h, (long)ofs, whence); }
static int f_close(void *h) { return fclose((FILE *)h); }
static long f_tell(void *h) { return ftell((FILE *)h); }

static const psf_file_callbacks g_callbacks = {
    "\\/", NULL, f_open, f_read, f_seek, f_close, f_tell
};

int main(int argc, char **argv)
{
    const char *in_path, *out_path;
    struct sdsf_loader_state state;
    void *emulator = NULL;
    FILE *out;
    int rval, psf_version;
    short buffer[MIN_BUFFER_SIZE * 2];
    int sample_rate = OUT_RATE;
    int max_buffer_samples = MIN_BUFFER_SIZE;
    int stream_samples_amount, fade_samples;
    int current_sample_pos = 0;
    long written = 0;

    if (argc < 3) {
        fprintf(stderr, "用法: refdump <input.ssf|.dsf> <output.pcm>\n");
        return EXIT_USAGE;
    }
    in_path = argv[1];
    out_path = argv[2];

    memset(&g_info, 0, sizeof(g_info));
    memset(&state, 0, sizeof(state));

    sega_init();

    /* ---- xsf_open(): 先读 meta 拿 length/fade ---- */
    psf_version = psf_load(in_path, &g_callbacks, 0, 0, 0, meta_cb, &g_info, 0,
                           NULL, NULL);
    if (psf_version <= 0) {
        fprintf(stderr, "ERR 读 meta 失败 (%d)\n", psf_version);
        return EXIT_PARSE;
    }

    /* ---- xsf_open(): 0x11 / 0x12 那一支 ---- */
    if (psf_load(in_path, &g_callbacks, (uint8_t)psf_version, sdsf_loader, &state,
                 0, 0, 0, NULL, NULL) <= 0) {
        fprintf(stderr, "ERR psf_load 失败\n");
        return EXIT_PARSE;
    }

    emulator = malloc(sega_get_state_size((uint8_t)(psf_version - 0x10)));
    if (!emulator) {
        free(state.data);
        return EXIT_NOMEM;
    }

    sega_clear_state(emulator, (uint8_t)(psf_version - 0x10));

    sega_enable_dry(emulator, 1);
    sega_enable_dsp(emulator, 1);

    sega_enable_dsp_dynarec(emulator, 0);

    {
        uint32_t start = get_le32(state.data);
        size_t length = state.data_size;
        const size_t max_length = (psf_version == 0x12) ? 0x800000 : 0x80000;
        if ((start + (length - 4)) > max_length)
            length = max_length - start + 4;
        sega_upload_program(emulator, state.data, (uint32_t)length);
    }

    free(state.data);

    /* ---- play() 的时长/淡出算法，原样 ---- */
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

    /* ---- play() 主循环，原样 ---- */
    while (1) {
        int toget = max_buffer_samples;
        unsigned count;

        if (current_sample_pos >= stream_samples_amount)
            break;
        if (current_sample_pos + toget > stream_samples_amount)
            toget = stream_samples_amount - current_sample_pos;

        /* xsf_render(): case 0x11 / 0x12 */
        count = (unsigned)toget;
        sega_execute(emulator, 0x7FFFFFFF, buffer, &count);

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
