/* refdump.c — PSF / minipsf / PSF2 / minipsf2（PS1/PS2 芯片音源）的
 *              「黄金参照」导出器。**只在开发机用，不进发布包。**
 *
 * 这份代码是 kode54 `audacious-xsf/plugin.cc` 里 PSF/PSF2 路径的**逐行移植**：
 *
 *   psf1_info（_refresh 标签）                    ← 原样（plugin.cc:520）
 *   psf1_load（PS1：upload PS-X EXE 到 IOP RAM）  ← 原样（plugin.cc:531）
 *   psf2fs_create / psf2fs_load_callback          ← 原样（plugin.cc:1101）
 *   virtual_readfile                              ← 原样（plugin.cc:565）
 *   xsf_open() 里 `psf_version == 1 || 2` 那一支   ← 原样（plugin.cc:1075）
 *   xsf_render() 里 `case 1 / case 2`             ← 原样（plugin.cc:1350）
 *   play() 里的时长/淡出算法                      ← 原样（plugin.cc:1438）
 *   parse_time_crap                               ← 原样（plugin.cc 同款）
 *
 * 唯一改掉的是 audacious 的 `write_audio()`，换成写裸 PCM；以及 BIOS 必须
 * 由参数 `--bios <path>` 给出（和 psf2wav 一致）。
 *
 * 为什么需要它
 * ------------
 * 判据必须是「逐样本最大差 == 0」。如果参照物就是我自己写的 `psf2wav.c`，
 * 那只是拿自己验自己。这里拿的是**上游作者的原始实现**当尺子 —— 容器解析、
 * psf2fs 文件树、`psx_execute` 的推进粒度、帧数换算、淡出斜坡，只要我抄错
 * 一处，样本就会差。
 *
 * 采样率
 * ------
 * `plugin.cc` 的 `get_srate(1)` 是 44100、`get_srate(2)` 是 48000，照抄。
 *
 * 用法
 * ----
 *   refdump <input.psf|.psf2> <output.pcm> --bios <BIOS>
 *
 * 退出码同 psf2wav。
 */
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include <psflib.h>
#include <psf2fs.h>

#include <psx.h>
#include <bios.h>
#include <iop.h>
#include <r3000.h>
#include <mkhebios.h>

#define OUT_CH          2
#define MIN_BUFFER_SIZE 576     /* ⚠ 必须和 plugin.cc 一致（plugin.cc:59） */
#define HE_BIOS_SIZE    0x80000u
#define PS2_BIOS_SIZE   0x400000u

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4,
    EXIT_NO_BIOS = 5
};

/* ------------------------------------------------------------------ */
/*  上游的 parse_time_crap                                             */
/* ------------------------------------------------------------------ */

#define BORK_TIME 0xC0FFEE

static uint32_t get_le32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8)
         | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static unsigned long parse_time_crap(const char *input)
{
    unsigned long value = 0;
    unsigned long multiplier = 1000;
    const char *ptr = input;
    unsigned long colon_count = 0;

    while (*ptr && ((*ptr >= '0' && *ptr <= '9') || *ptr == ':')) {
        colon_count += *ptr == ':';
        ++ptr;
    }
    if (colon_count > 2) return BORK_TIME;
    if (*ptr && *ptr != '.' && *ptr != ',') return BORK_TIME;
    if (*ptr) ++ptr;
    while (*ptr && *ptr >= '0' && *ptr <= '9') ++ptr;
    if (*ptr) return BORK_TIME;

    ptr = strrchr(input, ':');
    if (!ptr) ptr = input;
    for (;;) {
        char *end;
        if (ptr != input) ++ptr;
        if (multiplier == 1000) {
            double temp = strtod(ptr, &end);
            if (temp >= 60.0) return BORK_TIME;
            value = (long)(temp * 1000.0f);
        } else {
            unsigned long temp = strtoul(ptr, &end, 10);
            if (temp >= 60 && multiplier < 3600000) return BORK_TIME;
            value += temp * multiplier;
        }
        if (ptr == input) break;
        ptr -= 2;
        while (ptr > input && *ptr != ':') --ptr;
        multiplier *= 60;
    }

    return value;
}

/* ------------------------------------------------------------------ */
/*  上游的 psf1_load_state / psf1_info / psf1_load                     */
/* ------------------------------------------------------------------ */

struct psf1_load_state {
    void *emu;
    bool first;
    unsigned refresh;
};

static int psf1_info(void *context, const char *name, const char *value)
{
    struct psf1_load_state *state = (struct psf1_load_state *)context;

    if (!state->refresh && !strcasecmp(name, "_refresh")) {
        state->refresh = atoi(value);
    }

    return 0;
}

static int psf1_load(void *context, const uint8_t *exe, size_t exe_size,
                     const uint8_t *reserved, size_t reserved_size)
{
    struct psf1_load_state *state = (struct psf1_load_state *)context;

    (void)reserved;
    (void)reserved_size;

    if (exe_size < 0x800) return -1;

    uint32_t addr = get_le32(exe + 0x10);
    uint32_t size = (uint32_t)exe_size - 0x800;

    addr &= 0x1fffff;
    if ((addr < 0x10000) || (size > 0x1f0000) || (addr + size > 0x200000))
        return -1;

    void *pIOP = psx_get_iop_state(state->emu);
    iop_upload_to_ram(pIOP, addr, exe + 0x800, size);

    if (!state->refresh) {
        if (!strncasecmp((const char *)exe + 113, "Japan", 5))
            state->refresh = 60;
        else if (!strncasecmp((const char *)exe + 113, "Europe", 6))
            state->refresh = 50;
        else if (!strncasecmp((const char *)exe + 113, "North America", 13))
            state->refresh = 60;
    }

    if (state->first) {
        void *pR3000 = iop_get_r3000_state(pIOP);
        r3000_setreg(pR3000, R3000_REG_PC, get_le32(exe + 0x10));
        r3000_setreg(pR3000, R3000_REG_GEN + 29, get_le32(exe + 0x30));
        state->first = false;
    }

    return 0;
}

/* ------------------------------------------------------------------ */
/*  上游的 psf_info_meta（只留 length / fade）                          */
/* ------------------------------------------------------------------ */

struct info_state {
    int tag_song_ms;
    int tag_fade_ms;
};

static int info_meta(void *context, const char *name, const char *value)
{
    struct info_state *st = (struct info_state *)context;

    if (!strcasecmp(name, "length")) {
        int temp = (int)parse_time_crap(value);
        if (temp != BORK_TIME) {
            st->tag_song_ms = temp;
        }
    } else if (!strcasecmp(name, "fade")) {
        int temp = (int)parse_time_crap(value);
        if (temp != BORK_TIME) {
            st->tag_fade_ms = temp;
        }
    }
    return 0;
}

/* ---- virtual_readfile：PSF2 的 VFS 底层读取器 ---- */
static int EMU_CALL virtual_readfile(void *context, const char *path,
                                     int offset, char *buffer, int length)
{
    return psf2fs_virtual_readfile(context, path, offset, buffer, length);
}

/* ---- psflib 的文件回调 ---- */
static void *f_open(void *ctx, const char *path)
{
    (void)ctx;
    return (void *)fopen(path, "rb");
}
static size_t f_read(void *buf, size_t size, size_t count, void *h)
{
    return fread(buf, size, count, (FILE *)h);
}
static int f_seek(void *h, int64_t ofs, int whence)
{
    return fseek((FILE *)h, (long)ofs, whence);
}
static int f_close(void *h)
{
    return fclose((FILE *)h);
}
static long f_tell(void *h)
{
    return ftell((FILE *)h);
}

static const psf_file_callbacks g_callbacks = {
    "\\/", NULL, f_open, f_read, f_seek, f_close, f_tell
};

/* ------------------------------------------------------------------ */
/*  BIOS（和 psf2wav 同一套加载逻辑）                                  */
/* ------------------------------------------------------------------ */

static uint8_t *slurp(const char *path, long *size_out)
{
    FILE *f = fopen(path, "rb");
    long n;
    uint8_t *buf;

    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) || (n = ftell(f)) <= 0 || fseek(f, 0, SEEK_SET)) {
        fclose(f);
        return NULL;
    }
    buf = (uint8_t *)malloc((size_t)n);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) {
        free(buf);
        fclose(f);
        return NULL;
    }
    fclose(f);
    *size_out = n;
    return buf;
}

static uint8_t *load_he_bios(const char *path, uint32_t *size_out)
{
    long n = 0;
    uint8_t *buf = slurp(path, &n);

    if (!buf) return NULL;
    if (n == (long)PS2_BIOS_SIZE) {
        int osz = (int)n;
        void *he = mkhebios_create(buf, &osz);
        free(buf);
        if (!he) return NULL;
        *size_out = (uint32_t)osz;
        return (uint8_t *)he;
    }
    if (n == (long)HE_BIOS_SIZE) {
        *size_out = HE_BIOS_SIZE;
        return buf;
    }
    free(buf);
    return NULL;
}

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    const char *bios_path = NULL;
    int i;

    int psf_version = 0;
    void *emulator = NULL;
    void *emulator_extra = NULL;
    FILE *out = NULL;
    uint8_t *bios = NULL;
    uint32_t bios_size = 0;

    short buffer[MIN_BUFFER_SIZE * 2];
    int max_buffer_samples = MIN_BUFFER_SIZE;
    int stream_samples_amount, fade_samples;
    int current_sample_pos = 0;
    long written = 0;
    int rval;

    struct psf1_load_state state;
    struct info_state info;

    /*: length / fade 标签（毫秒），由 info_meta 填。 */

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--bios") && i + 1 < argc) {
            bios_path = argv[++i];
        } else if (!in_path) {
            in_path = argv[i];
        } else if (!out_path) {
            out_path = argv[i];
        }
    }
    if (!in_path || !out_path || !bios_path) {
        fprintf(stderr, "用法: refdump <in.psf|in.psf2> <out.pcm> --bios <BIOS>\n");
        return EXIT_USAGE;
    }

    /* ---- xsf_open(): 先读 meta 拿 length/fade ---- */
    memset(&info, 0, sizeof(info));
    rval = psf_load(in_path, &g_callbacks, 0, 0, 0, info_meta,
                    &info, 0, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr, "ERR 读 meta 失败 (%d)\n", rval);
        return EXIT_PARSE;
    }
    psf_version = rval;

    /* ---- BIOS 加载（HE 必需，且 psx_init 前） ---- */
    bios = load_he_bios(bios_path, &bios_size);
    if (!bios) {
        fprintf(stderr, "ERR BIOS 加载失败：%s\n", bios_path);
        return EXIT_NO_BIOS;
    }
    bios_set_image(bios, bios_size);
    if (psx_init() != 0) {
        fprintf(stderr, "ERR psx_init 失败\n");
        free(bios);
        return EXIT_PARSE;
    }

    if (psf_version == 1 || psf_version == 2) {
        emulator = malloc(psx_get_state_size(psf_version));
        if (!emulator) {
            fprintf(stderr, "ERR 分配模拟器状态失败\n");
            free(bios);
            return EXIT_NOMEM;
        }
        psx_clear_state(emulator, psf_version);

        memset(&state, 0, sizeof(state));
        state.first = true;

        if (psf_version == 1) {
            state.emu = emulator;
            if (psf_load(in_path, &g_callbacks, 1, psf1_load, &state,
                         psf1_info, &state, 1, NULL, NULL) <= 0) {
                fprintf(stderr, "ERR invalid PSF file\n");
                goto error;
            }
            if (state.refresh)
                psx_set_refresh(emulator, state.refresh);
        } else if (psf_version == 2) {
            emulator_extra = psf2fs_create();
            if (!emulator_extra) {
                fprintf(stderr, "ERR out of memory\n");
                goto error;
            }
            state.emu = emulator;
            if (psf_load(in_path, &g_callbacks, 2, psf2fs_load_callback,
                         emulator_extra, psf1_info, &state, 1, NULL, NULL) <= 0) {
                fprintf(stderr, "ERR invalid PSF file\n");
                goto error;
            }
            if (state.refresh)
                psx_set_refresh(emulator, state.refresh);

            psx_set_readfile(emulator, virtual_readfile, emulator_extra);
        }
    } else {
        fprintf(stderr, "ERR 不支持的 PSF 版本 %d\n", psf_version);
        goto error;
    }

    /* ---- play() 的时长/淡出算法，原样（plugin.cc:1438） ----
       没有 length 标签时的兜底：settings.fade_delay(170) +
       settings.fade_length(默认 10)，即 180 秒。 */
    {
        long long total_ms = info.tag_song_ms
            ? (long long)(info.tag_song_ms + info.tag_fade_ms)
            : (long long)(170.0 + 10.0) * 1000;
        long long fade_ms = info.tag_song_ms
            ? (long long)info.tag_fade_ms
            : (long long)(10.0 * 1000);
        int sample_rate = (psf_version == 2) ? 48000 : 44100;
        stream_samples_amount =
            (int)(total_ms * sample_rate / 1000LL);
        fade_samples =
            (int)(fade_ms * sample_rate / 1000LL);
    }

    out = fopen(out_path, "wb");
    if (!out) {
        fprintf(stderr, "ERR 无法写入 %s\n", out_path);
        goto error;
    }

    /* ---- play() 主循环，原样（去掉 check_stop / check_seek） ---- */
    while (1) {
        int toget = max_buffer_samples;

        if (current_sample_pos >= stream_samples_amount)
            break;
        if (current_sample_pos + toget > stream_samples_amount)
            toget = stream_samples_amount - current_sample_pos;

        /* xsf_render(): case 1 / case 2 */
        {
            unsigned count = (unsigned)toget;
            int err = psx_execute(emulator, 0x7FFFFFFF, buffer, &count, 0);
            if (err == -2) {
                fprintf(stderr, "ERR execution halted with an error\n");
                break;
            }
            /* 上游这里对 count 的语义：psx_execute 把 *count 改成实际生成数。
               plugin.cc 的 xsf_render 在 case 1/2 直接用的就是返回值填满
               count —— 我们照抄。 */
            toget = (int)count;
        }

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
    out = NULL;

    if (emulator_extra) psf2fs_delete(emulator_extra);
    if (emulator) free(emulator);
    free(bios);

    if (written == 0) {
        fprintf(stderr, "ERR 渲染出 0 个样本\n");
        return EXIT_EMPTY;
    }

    if (getenv("REFDUMP_VERBOSE"))
        fprintf(stderr, "OK %ld 帧 %.3f 秒\n",
                written, (double)written / (psf_version == 2 ? 48000.0 : 44100.0));

    return EXIT_OK;

error:
    if (out) fclose(out);
    if (emulator_extra) psf2fs_delete(emulator_extra);
    if (emulator) free(emulator);
    free(bios);
    return EXIT_PARSE;
}
