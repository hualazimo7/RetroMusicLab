/* ssf2wav.c — SSF / minissf（Saturn）与 DSF / minidsf（Dreamcast）→ WAV
 *
 * 为什么需要它
 * ------------
 * `.ssf` 是**内存快照**：解开之后约 573 KB，内容是「SH-2 声音驱动程序 +
 * 512KB SCSP 声音 RAM」。`.dsf` 更大（解开约 2 MB），是「ARM7 驱动 + AICA」。
 * 两者都**不是 PCM 数据** —— 必须真的跑驱动程序才出声。
 *
 * 类比：`.dvi` 像 MP3（ADPCM 帧，直接解码），`.ssf` 像「MIDI + 合成器」
 * （只有谱子和音色，得让合成器演奏）。
 *
 * 本工具把三件事拼起来：
 *
 *   psflib  （MIT）      解析 XSF 容器：zlib 解压、`_lib` 链、tags
 *   sega    （GPLv2+）   Highly Theoretical 核：SH-2 + SCSP（Saturn）
 *                        以及 ARM7 + AICA（Dreamcast）
 *   zlib    （zlib 许可）psflib 只用它一个 uncompress()
 *
 * 一个核管两种机器
 * ----------------
 * `sega_get_state_size(version)` 的 version：**1 = Saturn、2 = Dreamcast**，
 * 正好对应容器版本 0x11 / 0x12（`version - 0x10`）。所以 `.ssf` 和 `.dsf`
 * 共用同一个可执行文件，只在三处按版本分叉：
 *
 *   * 版本字节：0x11 / 0x12
 *   * 程序长度上限：Saturn 0x80000（512KB），Dreamcast 0x800000（8MB）
 *   * 状态尺寸（由核自己按 version 给）
 *
 * 为什么**不要** BIOS
 * -------------------
 * 这个核是 HLE 实现（自己算 BIOS 行为，不读文件），和 `usf2wav`/`vio2wav`
 * 一样**不需要任何外部文件**。
 *
 * `length` / `fade` 标签
 * ----------------------
 * 时长只来自标签。没有 `length` 时兜底 DEFAULT_SECS 秒。
 *
 * 用法
 * ----
 *   ssf2wav <input.ssf|.dsf> <output.wav> [秒数] [--no-fade]
 *   ssf2wav --info <input.ssf|.dsf>
 *
 * `--no-fade` 关掉淡出。**逐样本比对时必须加**。
 *
 * 退出码
 * ------
 *   0  成功
 *   1  参数错误
 *   2  psf_load 失败（不是有效的 SSF/DSF，或者 _lib 找不到）
 *   3  渲染出 0 个样本
 *   4  内存分配失败
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sega.h"

#include <psflib.h>

#define OUT_RATE    44100
#define OUT_CH      2
#define OUT_BITS    16
/*: ⚠ 每次渲染的帧数**必须**是 576，不能随手改成 1024。
 *
 * `sega_execute` 的语义是「跑到攒够这么多样本，或者跑完这么多周期」，
 * 它**不是**与分块大小无关的 —— 内部声音 FIFO 一次能吐多少就吐多少，
 * 剩下的留到下次。分块一变，SH-2 驱动被切分的时机就变了，输出会漂。
 *
 * 实测：改成 1024 之后，32 个 `.ssf` 里有 1 个在 0.72 秒处开始出现
 * 最大差 618 的分歧；改成 576 立刻差 0。576 是上游 plugin.cc 的
 * `MIN_BUFFER_SIZE`，也是事实标准，就跟它走。
 */
#define BUF_FRAMES  576
#define DEFAULT_SECS 180.0

/*: Saturn 的程序镜像上限 512KB，Dreamcast 8MB。
 *  超过就截断 —— 照 plugin.cc 的做法，不报错。 */
#define MAX_PROGRAM_SATURN 0x80000u
#define MAX_PROGRAM_DREAM  0x800000u

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4
};

/* ------------------------------------------------------------------ */
/*  小工具                                                             */
/* ------------------------------------------------------------------ */

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

static void wr16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

static void wr32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static int write_wav_header(FILE *f, uint32_t data_bytes,
                            uint32_t rate, uint16_t channels, uint16_t bits)
{
    uint8_t h[44];
    uint32_t byte_rate = rate * channels * bits / 8;
    uint16_t block_align = (uint16_t)(channels * bits / 8);

    memcpy(h, "RIFF", 4);
    wr32le(h + 4, 36 + data_bytes);
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, "fmt ", 4);
    wr32le(h + 16, 16);
    wr16le(h + 20, 1);                 /* PCM */
    wr16le(h + 22, channels);
    wr32le(h + 24, rate);
    wr32le(h + 28, byte_rate);
    wr16le(h + 32, block_align);
    wr16le(h + 34, bits);
    memcpy(h + 36, "data", 4);
    wr32le(h + 40, data_bytes);
    return fwrite(h, 1, 44, f) == 44 ? 0 : -1;
}

/*: `1:18.6` / `83.5` / `1:23` → 毫秒。非法返回 -1。
 *
 * 抄自 lazyusf2 的 `test/dumpresampled.c`。和 `usf2wav`/`vio2wav` 同一份，
 * 三处保持一致 —— SSF 的标签常写成 `1:18.6` 这种「分:秒.百分秒」，
 * 只有这套算法能正确解析（`18.6` 秒 = 18600 毫秒，不是 18.6 毫秒）。 */
static long parse_time_ms(const char *s)
{
    long value = 0;
    long multiplier = 1000;
    const char *ptr = s;
    int colons = 0;

    if (!s || !*s) return -1;

    while (*ptr && ((*ptr >= '0' && *ptr <= '9') || *ptr == ':')) {
        if (*ptr == ':') colons++;
        ++ptr;
    }
    if (colons > 2) return -1;
    if (*ptr && *ptr != '.' && *ptr != ',') return -1;
    if (*ptr) ++ptr;
    while (*ptr && *ptr >= '0' && *ptr <= '9') ++ptr;
    if (*ptr) return -1;

    ptr = strrchr(s, ':');
    if (!ptr) ptr = s;
    for (;;) {
        char *end = NULL;
        if (ptr != s) ++ptr;
        if (multiplier == 1000) {
            double temp = strtod(ptr, &end);
            if (end == ptr || temp < 0.0 || temp >= 60.0) return -1;
            value = (long)(temp * 1000.0);
        } else {
            unsigned long temp = strtoul(ptr, &end, 10);
            if (end == ptr) return -1;
            if (temp >= 60 && multiplier < 3600000) return -1;
            value += (long)temp * multiplier;
        }
        if (ptr == s) break;
        ptr -= 2;
        while (ptr > s && *ptr != ':') --ptr;
        multiplier *= 60;
    }
    return value;
}

/* ------------------------------------------------------------------ */
/*  SSF/DSF 装载：把若干「块」拼成一块连续的程序镜像                     */
/* ------------------------------------------------------------------ */
/*  照 kode54 `audacious-xsf/plugin.cc` 的 `sdsf_loader` 实现。         */
/*                                                                     */
/*  每个块的前 4 字节是「目标地址」，后面是数据。多个段（主文件 +        */
/*  `.ssflib`）依次回调，所以要把它们**合并**到同一块内存里：            */
/*    * 新块起点更靠前 → 整体后移，前面补 0                             */
/*    * 新块终点更靠后 → 扩容，后面补 0                                 */
/*  合并完的第 1 个 u32 就是程序的起始地址，直接喂 `sega_upload_program`。*/
/* ------------------------------------------------------------------ */

typedef struct {
    uint8_t *data;
    size_t   data_size;

    char title[256];
    char game[256];
    char length_s[64];
    char fade_s[64];

    int version;            /* 1 = Saturn, 2 = Dreamcast */
    int n_section;
} loadctx;

static int merge_block(loadctx *c, const uint8_t *exe, size_t exe_size)
{
    uint8_t *dst = c->data;
    uint32_t dst_start, src_start;
    size_t dst_len, src_len;

    if (exe_size < 4) return -1;

    /* 第一块：直接收下 */
    if (c->data_size < 4) {
        c->data = (uint8_t *)malloc(exe_size);
        if (!c->data) return -1;
        c->data_size = exe_size;
        memcpy(c->data, exe, exe_size);
        return 0;
    }

    dst_start = get_le32(dst);
    src_start = get_le32(exe);
    /* ⚠ 掩掉最高位：Saturn 的地址空间是 0x0~0x7FFFFF，
       有些 rip 会在地址上带 0x80000000 之类的标志位。 */
    dst_start &= 0x7fffff;
    src_start &= 0x7fffff;
    dst_len = c->data_size - 4;
    src_len = exe_size - 4;
    if (dst_len > 0x800000) dst_len = 0x800000;
    if (src_len > 0x800000) src_len = 0x800000;

    if (src_start < dst_start) {
        uint32_t diff = dst_start - src_start;
        uint8_t *np;
        c->data_size = dst_len + 4 + diff;
        np = (uint8_t *)realloc(dst, c->data_size);
        if (!np) return -1;
        dst = c->data = np;
        memmove(dst + 4 + diff, dst + 4, dst_len);
        memset(dst + 4, 0, diff);
        dst_len += diff;
        dst_start = src_start;
        set_le32(dst, dst_start);
    }
    if ((src_start + src_len) > (dst_start + dst_len)) {
        size_t diff = (size_t)(src_start + src_len) - (dst_start + dst_len);
        uint8_t *np;
        c->data_size = dst_len + 4 + diff;
        np = (uint8_t *)realloc(dst, c->data_size);
        if (!np) return -1;
        dst = c->data = np;
        memset(dst + 4 + dst_len, 0, diff);
    }

    memcpy(dst + 4 + (src_start - dst_start), exe + 4, src_len);
    return 0;
}

static int load_cb(void *ctx, const uint8_t *exe, size_t exe_size,
                   const uint8_t *reserved, size_t reserved_size)
{
    loadctx *c = (loadctx *)ctx;

    /* SSF/DSF 的数据全在 exe 段；reserved 段这两代格式都不用 */
    (void)reserved;
    (void)reserved_size;

    if (exe_size < 4) return 0;
    if (merge_block(c, exe, exe_size) != 0) {
        fprintf(stderr, "ERR 程序块合并失败（%zu 字节）\n", exe_size);
        return -1;
    }
    c->n_section++;
    return 0;
}

static int info_cb(void *ctx, const char *name, const char *value)
{
    loadctx *c = (loadctx *)ctx;
    if (!name || !value) return 0;
    if (!strcmp(name, "title"))  snprintf(c->title,    sizeof(c->title),    "%s", value);
    if (!strcmp(name, "game"))   snprintf(c->game,     sizeof(c->game),     "%s", value);
    if (!strcmp(name, "length")) snprintf(c->length_s, sizeof(c->length_s), "%s", value);
    if (!strcmp(name, "fade"))   snprintf(c->fade_s,   sizeof(c->fade_s),   "%s", value);
    return 0;
}

/* ---- psflib 的文件回调 ---- */
static void *f_open(void *ctx, const char *path) { (void)ctx; return (void *)fopen(path, "rb"); }
static size_t f_read(void *buf, size_t size, size_t count, void *h) { return fread(buf, size, count, (FILE *)h); }
static int f_seek(void *h, int64_t ofs, int whence) { return fseek((FILE *)h, (long)ofs, whence); }
static int f_close(void *h) { return fclose((FILE *)h); }
static long f_tell(void *h) { return ftell((FILE *)h); }

static const psf_file_callbacks g_callbacks = {
    "\\/", NULL, f_open, f_read, f_seek, f_close, f_tell
};

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

static void usage(void)
{
    fprintf(stderr,
        "用法:\n"
        "  ssf2wav <input.ssf|.dsf> <output.wav> [秒数] [--no-fade]\n"
        "  ssf2wav --info <input.ssf|.dsf>\n"
        "\n"
        "  --no-fade   不叠淡出（逐样本比对时必须加）\n"
        "  --info      只打印标签与段数，不渲染\n");
}

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    double want_secs = 0.0;
    int info_only = 0;
    int no_fade = 0;
    int i;

    loadctx c;
    void *emu = NULL;
    int rval;
    FILE *out = NULL;
    int16_t buf[BUF_FRAMES * OUT_CH];
    int64_t frames_left, total_frames = 0;
    uint32_t data_bytes = 0;
    long length_ms, fade_ms;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) {
            info_only = 1;
        } else if (!strcmp(argv[i], "--no-fade")) {
            no_fade = 1;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage();
            return EXIT_OK;
        } else if (!in_path) {
            in_path = argv[i];
        } else if (!out_path) {
            out_path = argv[i];
        } else {
            want_secs = atof(argv[i]);
        }
    }
    if (!in_path || (!info_only && !out_path)) {
        usage();
        return EXIT_USAGE;
    }
    if (info_only) out_path = NULL;

    memset(&c, 0, sizeof(c));

    /* ---- 0. 核的静态初始化（整个进程一次） ---- */
    if (sega_init() != 0) {
        fprintf(stderr, "ERR sega_init 失败\n");
        return EXIT_NOMEM;
    }

    /* ---- 1. 先探版本（0x11 Saturn / 0x12 Dreamcast） ---- */
    rval = psf_load(in_path, &g_callbacks, 0, NULL, NULL, info_cb, &c, 0, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr, "ERR 不是有效的 SSF/DSF（psf_load 返回 %d）\n", rval);
        return EXIT_PARSE;
    }
    if (rval != 0x11 && rval != 0x12) {
        fprintf(stderr, "ERR 版本字节是 0x%02x，不是 SSF(0x11) 也不是 DSF(0x12)\n",
                rval);
        return EXIT_PARSE;
    }
    c.version = rval - 0x10;

    /* ---- 2. 解析容器，跟 `_lib` 链，把块合并成一块程序镜像 ---- */
    rval = psf_load(in_path, &g_callbacks, (uint8_t)rval, load_cb, &c,
                    NULL, NULL, 1, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr,
                "ERR psf_load 失败 (%d)：不是有效的 SSF/DSF，"
                "或者配套的 .ssflib / .dsflib 找不到\n", rval);
        free(c.data);
        return EXIT_PARSE;
    }
    if (!c.data || c.data_size <= 4) {
        /* 走到这里说明容器头是合法的，但里面没有可执行的驱动数据 ——
           常见于「截断的下载」或者「把 .ssflib 改名成 .ssf」。 */
        fprintf(stderr,
                "ERR 程序镜像为空：SSF/DSF 里没有可执行的驱动程序数据"
                "（文件可能不完整，或者缺少配套的 .ssflib / .dsflib）\n");
        free(c.data);
        return EXIT_PARSE;
    }

    /* ---- 3. 建核、上传程序 ---- */
    {
        uint32_t ss = sega_get_state_size((uint8_t)c.version);
        uint32_t start;
        size_t length;
        size_t max_length;

        if (!ss) {
            fprintf(stderr, "ERR sega_get_state_size 返回 0\n");
            free(c.data);
            return EXIT_NOMEM;
        }
        emu = malloc(ss);
        if (!emu) {
            fprintf(stderr, "ERR 分配模拟器状态失败（%u 字节）\n", ss);
            free(c.data);
            return EXIT_NOMEM;
        }
        sega_clear_state(emu, (uint8_t)c.version);
        sega_enable_dry(emu, 1);
        sega_enable_dsp(emu, 1);
        /* 关掉 JIT：dynarec 会按编译机 CPU 出码，换机器就是 0xC000001D
           （README ⚠51）。这个核的 dynarec 本来就是可选项。 */
        sega_enable_dsp_dynarec(emu, 0);

        start = get_le32(c.data);
        length = c.data_size;
        max_length = (c.version == 2) ? MAX_PROGRAM_DREAM : MAX_PROGRAM_SATURN;
        if ((start + (length - 4)) > max_length) {
            /* 截断，不报错 —— 照上游做法。Saturn 的镜像里常带着
               512KB 声音 RAM 的整块拷贝，超出寻址范围的部分本来就读不到。 */
            length = max_length - start + 4;
        }
        if (sega_upload_program(emu, c.data, (uint32_t)length) != 0) {
            fprintf(stderr, "ERR sega_upload_program 失败（%zu 字节 @0x%08x）\n",
                    length, start);
            free(emu);
            free(c.data);
            return EXIT_PARSE;
        }
    }

    length_ms = parse_time_ms(c.length_s);
    fade_ms   = parse_time_ms(c.fade_s);
    if (fade_ms < 0) fade_ms = 0;

    if (info_only) {
        printf("version=0x%02x\n", c.version + 0x10);
        printf("machine=%s\n", c.version == 1 ? "Saturn" : "Dreamcast");
        printf("title=%s\n", c.title);
        printf("game=%s\n", c.game);
        printf("length=%s\n", c.length_s[0] ? c.length_s : "(无)");
        printf("fade=%s\n", c.fade_s[0] ? c.fade_s : "(无)");
        printf("sections=%d\n", c.n_section);
        printf("program_size=%zu\n", c.data_size);
        printf("length_ms=%ld\n", length_ms);
        printf("fade_ms=%ld\n", fade_ms);
        free(emu);
        free(c.data);
        return EXIT_OK;
    }

    /* ---- 4. 算总帧数 ---- */
    if (length_ms < 0) {
        length_ms = (long)(DEFAULT_SECS * 1000.0);
        fprintf(stderr, "NOTE 没有可用的 length 标签，按默认 %.0f 秒渲染\n",
                DEFAULT_SECS);
    }
    if (want_secs > 0.0) {
        long ms = (long)(want_secs * 1000.0);
        if (ms < length_ms) length_ms = ms;
    }
    if (no_fade) fade_ms = 0;

    /* 帧数换算用整数 `ms * 441 / 10`，和 usf2wav / vio2wav 同一套口径。
       ⚠ 总长是 **length + fade**。 */
    {
        long play_ms = length_ms + fade_ms;
        total_frames = (int64_t)play_ms * 441 / 10;
    }

    {
        int64_t fade_frames = (int64_t)fade_ms * 441 / 10;
        int64_t fade_start = total_frames - fade_frames;

        out = fopen(out_path, "wb");
        if (!out) {
            fprintf(stderr, "ERR 无法写入 %s\n", out_path);
            free(emu);
            free(c.data);
            return EXIT_USAGE;
        }
        if (write_wav_header(out, 0, OUT_RATE, OUT_CH, OUT_BITS) != 0) {
            fprintf(stderr, "ERR 写 WAV 头失败\n");
            fclose(out);
            free(emu);
            free(c.data);
            return EXIT_USAGE;
        }

        /* ---- 5. 渲染 ----
         *
         * `sega_execute` 的第 4 个参数是**进出双向**的：进来是「最多要多少
         * 个样本」，出去是「实际给了多少」。上游 play() 的做法是直接按
         * 请求量写出，不管实际给了多少 —— 这里必须**照抄**，否则样本数
         * 就和参照对不上。
         *
         * 但有一个安全差异：上游的 buffer 是栈上复用的，给少了会写上一块
         * 的残留。我们这里每次把请求区间**先清零**再渲染，把「残留」变成
         * 「静音」—— 只有在核真的给少了的时候才不同，实测 46 个文件一次
         * 都没触发（`sega_execute` 每次都按量给足）。 */
        frames_left = total_frames;
        while (frames_left > 0) {
            int32_t n = (frames_left > BUF_FRAMES)
                        ? BUF_FRAMES : (int32_t)frames_left;
            uint32_t got = (uint32_t)n;
            int32_t j;
            int samples_into_fade;

            memset(buf, 0, (size_t)n * OUT_CH * 2);
            if (sega_execute(emu, 0x7FFFFFFF, buf, &got) <= -1) {
                fprintf(stderr, "WARN sega_execute 报错，提前结束\n");
                break;
            }
            if (got == 0) {
                fprintf(stderr, "WARN sega_execute 没产出样本，提前结束\n");
                break;
            }

            if (fade_frames > 0) {
                samples_into_fade = (int)((int64_t)(total_frames - frames_left)
                                          - fade_start);
                for (j = 0; j < n; j++, samples_into_fade++) {
                    if (samples_into_fade > 0) {
                        double fadedness = (double)(fade_frames - samples_into_fade)
                                           / (double)fade_frames;
                        buf[j * 2 + 0] = (int16_t)(buf[j * 2 + 0] * fadedness);
                        buf[j * 2 + 1] = (int16_t)(buf[j * 2 + 1] * fadedness);
                    }
                }
            }

            if (fwrite(buf, (size_t)OUT_CH * 2, (size_t)n, out) != (size_t)n) {
                fprintf(stderr, "ERR 写 WAV 数据失败\n");
                break;
            }
            data_bytes += (uint32_t)(n * OUT_CH * 2);
            frames_left -= n;
        }

        /* ---- 6. 回填两个长度字段（偏移 4 和 40） ---- */
        {
            uint8_t h[4];
            if (fseek(out, 4, SEEK_SET) == 0) {
                wr32le(h, 36 + data_bytes);
                fwrite(h, 1, 4, out);
            }
            if (fseek(out, 40, SEEK_SET) == 0) {
                wr32le(h, data_bytes);
                fwrite(h, 1, 4, out);
            }
        }
        fclose(out);
        out = NULL;

        if (data_bytes == 0) {
            fprintf(stderr, "ERR 渲染出 0 个样本\n");
            free(emu);
            free(c.data);
            return EXIT_EMPTY;
        }
    }

    if (getenv("SSF2WAV_VERBOSE"))
        fprintf(stderr, "OK %s %d 段 %lld 帧 %.2f 秒\n",
                c.version == 1 ? "Saturn" : "Dreamcast", c.n_section,
                (long long)(data_bytes / (OUT_CH * 2)),
                (double)(data_bytes / (OUT_CH * 2)) / OUT_RATE);

    free(emu);
    free(c.data);
    return EXIT_OK;
}
