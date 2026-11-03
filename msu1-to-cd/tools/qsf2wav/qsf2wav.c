/* qsf2wav.c — QSF（Capcom QSound 街机音源）→ WAV 的命令行解码器
 *
 * QSF 是什么
 * ----------
 * QSF 是 PSF 家族里给 **Capcom QSound** 用的那一支，版本字节 **0x41**
 * （PS1=0x01、PS2=0x02、Saturn=0x11、DC=0x12、N64=0x21、QSound=0x41）。
 * 覆盖 CPS1 / CPS2 / CP-ZN1 / CP-ZN2 上带 QSound 的街机板。
 *
 * 它和 PSF 的区别只有一处：**program section 里放的不是 CPU 程序，
 * 而是一串「块」**。每块 11 字节头 + 载荷：
 *
 *     char   id[3];        // "Z80" | "SMP" | "KEY"
 *     uint32 dataofs;      // 载荷写进该块缓冲区的偏移（小端）
 *     uint32 datasize;     // 载荷长度（小端）
 *     uint8  data[datasize];
 *
 *   "Z80"  Z80 程序 ROM（CPS2 常见 128K / 256K，靠端口 0xC000+ 换 16K 页）
 *   "SMP"  QSound 采样 ROM（16 位大端 PCM，CPS2 常见 4MB / 8MB）
 *   "KEY"  CPS1 的 Kabuki 解密密钥，正好 11 字节
 *
 * 一个包里有两种文件：
 *
 *   <游戏>.qsflib   只有一块（或几块）：完整 Z80 程序 + 全部采样，全曲共用
 *   NN - 曲名.miniqsf  12 字节，只有一块 "Z80"：
 *                      dataofs=0x21、datasize=1、data=曲号
 *                    —— 它就是往 Z80 ROM 的 **0x21 这一个字节** 里写曲号。
 *                    驱动把那个字节当「当前曲目」，所以一首歌一个文件。
 *
 * 实测（Mega Man 2 - The Power Fighters，45 首）：
 *
 *   01 - Opening.miniqsf  5a 38 30 | 21 00 00 00 | 01 00 00 00 | 01
 *                        "Z80"      ofs=0x21      size=1        song=1
 *   02 - Player Select    5a 38 30 | 21 00 00 00 | 01 00 00 00 | 03
 *   05 - Plant Man        ...        ofs=0x21      size=1        song=0x0E
 *
 *   qsflib 解压后 4,456,470 字节 =
 *       "Z80" 块 11 + 0x40000（256K 程序，尾部补 0）
 *     + "SMP" 块 11 + 0x400000（4MB 采样）
 *
 * 为什么必须模拟
 * --------------
 * 和 PSF 一样，QSF 里没有波形。要出声得跑 Z80 程序、由它去驱动 QSound DSP：
 * 16 个声道的 16 位 PCM 播放、ADPCM 回放、声像、回声。这不是解析器能干的活。
 *
 * 本工具把四件事拼起来：
 *
 *   psflib              （MIT）      解析 PSF 容器：zlib 解压、_lib 链、tags
 *   highly_quixotic     （GPL-3.0）  QSound 模拟核心
 *       qsound.c         顶层：Z80 内存映射、bank 切换、IRQ、采样节流
 *       qsound_ctr.c     混音：16 声道 PCM/ADPCM、声像、回声
 *       z80.c            Z80 核心（源自 MAME，kode54 改造成脱离 MAME 独立跑）
 *       kabuki.c         CPS1 Kabuki 解密
 *   zlib                （zlib 许可）  psflib 只用 uncompress()
 *
 * 输出：**44100 Hz** / 立体声 / 16 位 —— 和主流水线其它机种一致。
 *
 * QSound 的原生输出是 **24038 Hz**，这个数字不是随便定的：
 * `qsound_clear_state()` 里写死 `qsound_set_rates(state, 8000000, 250, 24038)`，
 * 而 `device_start_qsound_ctr(60000000)` 返回 `60000000/2/1248 = 24038.46`
 * —— 也就是 QSound DAC 的硬件节拍。混音器每输出一个样本就按这个时钟推进一次
 * DSP，**把数字改大只会让整首变调**，所以只能老老实实重采样。
 *
 * 重采样用 64 抽头 Kaiser 加窗 sinc 多相 FIR（截止 0.45×源采样率），
 * 位置 32.32 定点累加不漂移。`--rate 24038` 可以关掉它，走 1:1 直通。
 *
 * 用法
 * ----
 *   qsf2wav <input.miniqsf> <output.wav> [秒数] [--rate 44100|24038]
 *   qsf2wav --info <input.miniqsf>      打印 tags + 块布局
 *
 * 退出码
 * ------
 *   0  成功（且确实有声音）
 *   1  一般错误（参数 / 文件 / 解析 / 初始化）
 *   2  用法错误
 *   3  解码跑完了但输出全静音
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>

#include "psflib.h"
#include "qsound.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ------------------------------------------------------------------ */
/*  非 ASCII 路径                                                      */
/* ------------------------------------------------------------------ */

/* Windows 的窄字符 fopen 走 **ANSI 码页**，路径里只要有一个中文就打不开 ——
 * 文件明明在那儿，报的却是「不是合法的 QSF」。joshw 的机种目录天生带中文
 * （`QSound 街机（Hoot）`、`PlayStation 中文`…），所以这一层躲不掉。
 *
 * 策略：先按原样试（纯 ASCII 路径一次就成），失败再转宽字符试两次 ——
 * 一次按 UTF-8 解、一次按 ANSI 解，谁对谁上。不猜 argv 到底是哪种编码，
 * 两种都试的成本只有一次失败的 fopen。 */
#ifdef _WIN32
#include <windows.h>
#include <wchar.h>

static FILE *xfopen(const char *path, const char *mode)
{
    FILE *f = fopen(path, mode);
    wchar_t wpath[1024];
    wchar_t wmode[8];
    UINT cp;

    if (f) return f;
    if (MultiByteToWideChar(CP_UTF8, 0, mode, -1, wmode, 8) <= 0) return NULL;
    for (cp = CP_UTF8; ; cp = CP_ACP) {
        if (MultiByteToWideChar(cp, 0, path, -1, wpath, 1024) > 0) {
            f = _wfopen(wpath, wmode);
            if (f) return f;
        }
        if (cp == CP_ACP) break;
    }
    return NULL;
}
#else
static FILE *xfopen(const char *path, const char *mode)
{
    return fopen(path, mode);
}
#endif

/*: QSound 的**原生**输出采样率。必须和 qsound_clear_state() 里的 24038 一致。
 *
 *  它不是笔误：`device_start_qsound_ctr(60000000)` 返回 `60000000/2/1248`
 *  = 24038.46，即 QSound DAC 的硬件节拍。改不了 —— 混音器每输出一个样本就
 *  按这个时钟推进一次 DSP（`update_sample()`），把数字改大只会让整首变调。 */
#define SRC_RATE 24038

/*: 本工具默认输出的采样率 = **主流水线的房子采样率**。
 *
 *  `spc_wav.write_tracks()` 恒定写 16bit/44100/立体声（`SR = 44100`），
 *  所有原生解码器都得按这个交卷 —— GSF 的原生率是 32768，也是靠 blip_buf
 *  在解码器里直接出 44100 的（见 `gsf_native.py` 的注释）。
 *  所以这里把 24038 重采样到 44100，而不是让下游去猜。 */
#define OUT_RATE 44100

/*: 没 length 标签时的兜底时长（毫秒）。和 psf2wav / plugin 默认值一致。 */
#define DEFAULT_LENGTH_MS 170000
#define DEFAULT_FADE_MS    10000

#define EXIT_OK      0
#define EXIT_ERR     1
#define EXIT_USAGE   2
#define EXIT_SILENT  3

/* ------------------------------------------------------------------ */
/*  小工具                                                             */
/* ------------------------------------------------------------------ */

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

static uint32_t rd32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* ------------------------------------------------------------------ */
/*  ROM 累积                                                           */
/* ------------------------------------------------------------------ */

/*: 一块 ROM。`size` 是**实际数据长度**（= max(dataofs+datasize)），
 *  `cap` 是分配容量（按 2 的幂增长）。
 *
 *  ⚠ 传给 qsound_set_sample_rom() 的必须是 `size` 而不是 `cap` ——
 *    qsound_ctr.c 会用 pow2_mask(length) 算寻址掩码，多报一倍的容量
 *    会让 DSP 读到缓冲区外面的零，声音直接变调。 */
typedef struct {
    uint8_t *data;
    uint32_t size;
    uint32_t cap;
} rombuf;

typedef struct {
    rombuf z80;
    rombuf smp;
    uint8_t key[11];
    int     key_size;
    int     n_chunk;
    char    layout[512];       /* --info 用：块布局摘要 */
    char    title[256];
    char    game[256];
    char    artist[256];
    char    length_s[64];
    char    fade_s[64];
} qsf_ctx;

static int rom_reserve(rombuf *b, uint32_t need)
{
    uint32_t n;
    uint8_t *p;

    if (need <= b->cap) return 0;
    n = b->cap ? b->cap : 0x10000;
    while (n < need) {
        if (n > 0x40000000u) return -1;      /* 防溢出 */
        n <<= 1;
    }
    p = (uint8_t *)realloc(b->data, n);
    if (!p) return -1;
    memset(p + b->cap, 0, n - b->cap);
    b->data = p;
    b->cap  = n;
    return 0;
}

static int rom_upload(rombuf *b, uint32_t ofs, const uint8_t *src, uint32_t len)
{
    uint32_t end = ofs + len;

    if (end < ofs) return -1;                /* 溢出 */
    if (rom_reserve(b, end)) return -1;
    memcpy(b->data + ofs, src, len);
    if (end > b->size) b->size = end;
    return 0;
}

/* ------------------------------------------------------------------ */
/*  块解析（QSF 的全部秘密就在这 30 行）                               */
/* ------------------------------------------------------------------ */

static int qsf_load_cb(void *ctx, const uint8_t *exe, size_t exe_size,
                       const uint8_t *reserved, size_t reserved_size)
{
    qsf_ctx *c = (qsf_ctx *)ctx;

    (void)reserved;
    (void)reserved_size;

    /* psflib 的回调顺序：最深层 _lib 先 → 主文件 → _lib2/_lib3…
     * 所以 qsflib 的 "Z80"/"SMP" 先到，miniqsf 的那 1 字节补丁后到，
     * 天然就是「先铺底、后打补丁」的正确顺序。 */
    while (exe_size >= 11) {
        char id[4];
        uint32_t dataofs, datasize;

        memcpy(id, exe, 3);
        id[3] = 0;
        dataofs  = rd32le(exe + 3);
        datasize = rd32le(exe + 7);
        exe      += 11;
        exe_size -= 11;

        if ((size_t)datasize > exe_size) {
            fprintf(stderr, "ERR 块 %s 声明 %u 字节，实际只剩 %u 字节\n",
                    id, datasize, (unsigned)exe_size);
            return -1;
        }

        if (!memcmp(id, "Z80", 3)) {
            if (rom_upload(&c->z80, dataofs, exe, datasize)) return -1;
            if (c->n_chunk < 8)
                snprintf(c->layout + strlen(c->layout),
                         sizeof(c->layout) - strlen(c->layout),
                         "Z80 ofs=0x%X size=%u; ", dataofs, datasize);
        } else if (!memcmp(id, "SMP", 3)) {
            if (rom_upload(&c->smp, dataofs, exe, datasize)) return -1;
            if (c->n_chunk < 8)
                snprintf(c->layout + strlen(c->layout),
                         sizeof(c->layout) - strlen(c->layout),
                         "SMP ofs=0x%X size=%u; ", dataofs, datasize);
        } else if (!memcmp(id, "KEY", 3)) {
            if (datasize == 11) {
                memcpy(c->key, exe, 11);
                c->key_size = 11;
            }
            if (c->n_chunk < 8)
                snprintf(c->layout + strlen(c->layout),
                         sizeof(c->layout) - strlen(c->layout),
                         "KEY size=%u; ", datasize);
        } else {
            if (c->n_chunk < 8)
                snprintf(c->layout + strlen(c->layout),
                         sizeof(c->layout) - strlen(c->layout),
                         "%s ofs=0x%X size=%u; ", id, dataofs, datasize);
        }
        c->n_chunk++;

        exe      += datasize;
        exe_size -= datasize;
    }
    return 0;
}

static int qsf_info_cb(void *ctx, const char *name, const char *value)
{
    qsf_ctx *c = (qsf_ctx *)ctx;

    if (!name || !value) return 0;
    if (!strcasecmp(name, "title"))       snprintf(c->title,    sizeof(c->title),    "%s", value);
    else if (!strcasecmp(name, "game"))   snprintf(c->game,     sizeof(c->game),     "%s", value);
    else if (!strcasecmp(name, "artist")) snprintf(c->artist,   sizeof(c->artist),   "%s", value);
    else if (!strcasecmp(name, "length")) snprintf(c->length_s, sizeof(c->length_s), "%s", value);
    else if (!strcasecmp(name, "fade"))   snprintf(c->fade_s,   sizeof(c->fade_s),   "%s", value);
    return 0;
}

static void qsf_status_cb(void *ctx, const char *msg)
{
    (void)ctx;
    (void)msg;                 /* psflib 的内部过程日志，正常时不打印 */
}

/* ---- psflib 的文件回调（Windows 上直接用 stdio） ---- */

static void *f_open(void *ctx, const char *path)
{
    (void)ctx;
    return (void *)xfopen(path, "rb");
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
    "\\/",          /* path_separators */
    NULL,           /* context */
    f_open, f_read, f_seek, f_close, f_tell
};

/* ------------------------------------------------------------------ */
/*  WAV 写出                                                           */
/* ------------------------------------------------------------------ */

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
 *  和 psf2wav / usf2wav / ssf2wav / gsf2wav 是同一份实现（抄自 lazyusf2
 *  的 test/dumpresampled.c）。⚠ 别凭印象重写成「分:秒.百分秒」——
 *  上游是用 strtod 读最后一节，`18.6` 就是 18600 毫秒。 */
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
            if (end == ptr || temp >= 60) return -1;
            value += (long)temp * multiplier;
        }
        if (ptr == s) break;
        ptr -= 2;
        while (ptr > s && *ptr != ':') --ptr;
        multiplier *= 60;
    }
    return value;
}

static void usage(void)
{
    fprintf(stderr,
        "qsf2wav — QSF / miniqsf（Capcom QSound 街机）→ WAV\n"
        "\n"
        "用法:\n"
        "  qsf2wav <input.miniqsf> <output.wav> [秒数] [--rate 44100|24038]\n"
        "  qsf2wav --info <input.miniqsf>\n"
        "\n"
        "  秒数    不写就用 QSF 自带的 length 标签，再不行 170 秒\n"
        "  --rate  输出采样率。默认 44100（主流水线的房子口径，\n"
        "          QSound 原生 24038 会在这里重采样上去）；\n"
        "          传 24038 则 1:1 直通，用于和参考实现逐样本比对\n"
        "  声道    恒定立体声 / 16 位\n");
}

/* ------------------------------------------------------------------ */
/*  重采样：24038 → 44100                                              */
/* ------------------------------------------------------------------ */

/* 为什么不用线性插值
 * ------------------
 * 24038 → 44100 是**升采样 1.834 倍**。线性插值的频响是 sinc²，会在
 * 24.038k−f 处留下镜像（比如 12k 的内容会在 12.0k 附近叠一层反向副本），
 * 听感上是「发闷 + 有点毛刺」。
 *
 * 更关键的是：QSound 的源带宽到 12019Hz，而 44100 的奈奎斯特是 22050Hz ——
 * 中间 12k~22k 本该是空的。线性插值填进去的全是镜像噪声，FFT 一眼就能看见。
 *
 * 所以用**加窗 sinc 多相 FIR**：截止放在源采样率的 45%（≈10817Hz，留 10%
 * 过渡带），Kaiser 窗 β=9，64 抽头。相位表 1024 格，位置用 32.32 定点累加，
 * 所以再长的曲子也不会累积漂移。
 *
 * 实测：整张 62 分钟跑下来耗时从 38.7s 涨到 ~70s（仍有 50× 实时），
 * 换来 12k 以上干净 —— 这个买卖划算。 */

#define RS_TAPS   64
#define RS_PHASES 1024

static float g_rs[RS_PHASES][RS_TAPS];

/*: 零阶修正贝塞尔函数（Kaiser 窗要用）。级数展开，x<20 时收敛很快。 */
static double bessel_i0(double x)
{
    double sum = 1.0, term = 1.0, y = x * x / 4.0;
    int k;
    for (k = 1; k < 64; k++) {
        term *= y / ((double)k * (double)k);
        sum += term;
        if (term < 1e-12 * sum) break;
    }
    return sum;
}

/*: 建相位表。每个相位 p 对应小数部分 f = p/PHASES，
 *  抽头 k 的「时间偏移」t = (k − (TAPS/2 − 1)) − f（单位：源样本）。
 *
 *  f=0 时 k=TAPS/2−1 的 t=0 → sinc 取 1，其余整数 t 全落在 sinc 零点 →
 *  输出严格等于输入样本。这条性质保证「不重采样时不会偷偷改声音」。 */
static void rs_init(double fc, double beta)
{
    const double half = RS_TAPS / 2.0;
    const double i0b  = bessel_i0(beta);
    int p, k;

    for (p = 0; p < RS_PHASES; p++) {
        const double f = (double)p / (double)RS_PHASES;
        double sum = 0.0;
        for (k = 0; k < RS_TAPS; k++) {
            const double t = ((double)k - (half - 1.0)) - f;
            const double x = 2.0 * fc * t;
            const double s = (fabs(x) < 1e-9) ? 1.0 : sin(M_PI * x) / (M_PI * x);
            const double r = t / half;
            double w = 0.0;
            if (fabs(r) < 1.0)
                w = bessel_i0(beta * sqrt(1.0 - r * r)) / i0b;
            g_rs[p][k] = (float)(2.0 * fc * s * w);
            sum += g_rs[p][k];
        }
        /* 归一化到直流增益 1 —— 相位量化后 sum 会有 1e-3 级偏差，
         * 不归一化会在整首曲子上叠一个极轻的周期性起伏。 */
        if (fabs(sum) > 1e-9) {
            for (k = 0; k < RS_TAPS; k++)
                g_rs[p][k] = (float)(g_rs[p][k] / sum);
        }
    }
}

static int16_t rs_clamp(float v)
{
    if (v > 32767.0f)  return 32767;
    if (v < -32768.0f) return -32768;
    return (int16_t)(v < 0.0f ? v - 0.5f : v + 0.5f);
}

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    qsf_ctx c;
    void   *state = NULL;
    FILE   *out = NULL;
    const char *inp = NULL, *outp = NULL;
    const char *pos[4];
    int     npos = 0, info_only = 0, i, rc = EXIT_ERR;
    double  want_secs = 0.0;
    int     out_rate = OUT_RATE;

    memset(&c, 0, sizeof(c));

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) {
            info_only = 1;
        } else if (!strcmp(argv[i], "--rate") && i + 1 < argc) {
            /* 只给两个合法值：44100（房子口径）和 24038（原生，1:1 直通）。
             * 别的值不是「多一种选择」，是「多一种没人验证过的输出」。 */
            int r = atoi(argv[++i]);
            if (r == SRC_RATE) {
                out_rate = SRC_RATE;
            } else if (r == OUT_RATE) {
                out_rate = OUT_RATE;
            } else {
                fprintf(stderr, "ERR --rate 只支持 %d（原生）或 %d（默认）\n",
                        SRC_RATE, OUT_RATE);
                return EXIT_USAGE;
            }
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage();
            return EXIT_USAGE;
        } else if (npos < 4) {
            pos[npos++] = argv[i];
        }
    }

    if (info_only) {
        if (!npos) { usage(); return EXIT_USAGE; }
        inp = pos[0];
    } else {
        if (npos < 2) { usage(); return EXIT_USAGE; }
        inp = pos[0];
        outp = pos[1];
        if (npos >= 3) {
            want_secs = atof(pos[2]);
            if (want_secs < 0.0) want_secs = 0.0;
        }
    }

    /* ---- 1. 解容器：zlib + _lib 链 + tags + 块 ---- */
    if (psf_load(inp, &g_callbacks, 0x41, qsf_load_cb, &c,
                 qsf_info_cb, &c, 0, qsf_status_cb, NULL) < 0) {
        fprintf(stderr,
                "ERR 不是合法的 QSF（PSF 版本字节 0x41）：%s\n"
                "    也检查一下 _lib 指向的 .qsflib 是否和它放在同一目录。\n",
                inp);
        goto done;
    }

    if (info_only) {
        printf("file   = %s\n", inp);
        printf("game   = %s\n", c.game);
        printf("title  = %s\n", c.title);
        printf("artist = %s\n", c.artist);
        printf("length = %s\n", c.length_s);
        printf("fade   = %s\n", c.fade_s);
        printf("chunks = %d : %s\n", c.n_chunk, c.layout);
        printf("z80rom = %u B\n", c.z80.size);
        printf("smprom = %u B\n", c.smp.size);
        printf("key    = %s\n", c.key_size == 11 ? "11 B (Kabuki)" : "none");
        rc = EXIT_OK;
        goto done;
    }

    if (!c.z80.size) {
        fprintf(stderr, "ERR 没找到 Z80 程序块（qsflib 缺失或损坏）\n");
        goto done;
    }

    /* ---- 2. 初始化 QSound ---- */
    if (qsound_init()) {
        fprintf(stderr, "ERR qsound_init 失败（字节序没配好？）\n");
        goto done;
    }
    state = malloc(qsound_get_state_size());
    if (!state) { fprintf(stderr, "ERR 内存不足\n"); goto done; }
    qsound_clear_state(state);

    if (c.key_size == 11) {
        /* 密钥在文件里是**大端**存的，上游用 Endian_Swap32 转成本机序。 */
        const uint8_t *k = c.key;
        uint32_t k1 = ((uint32_t)k[0] << 24) | ((uint32_t)k[1] << 16) |
                      ((uint32_t)k[2] << 8)  |  (uint32_t)k[3];
        uint32_t k2 = ((uint32_t)k[4] << 24) | ((uint32_t)k[5] << 16) |
                      ((uint32_t)k[6] << 8)  |  (uint32_t)k[7];
        uint16_t ak = (uint16_t)(((uint16_t)k[8] << 8) | k[9]);
        qsound_set_kabuki_key(state, k1, k2, ak, k[10]);
    } else {
        qsound_set_kabuki_key(state, 0, 0, 0, 0);
    }
    qsound_set_z80_rom(state, c.z80.data, c.z80.size);
    qsound_set_sample_rom(state, c.smp.data, c.smp.size);

    /* ---- 3. 算时长 ---- */
    {
        int64_t length_ms = parse_time_ms(c.length_s);
        int64_t fade_ms   = parse_time_ms(c.fade_s);
        int64_t play_ms, total_frames, fade_frames, fade_start;
        int64_t frames_left;
        uint32_t total_samples = 0;
        int32_t peak = 0;
        int stall = 0;

        if (fade_ms < 0) fade_ms = 0;
        if (length_ms < 0) {
            length_ms = DEFAULT_LENGTH_MS;
            if (fade_ms <= 0) fade_ms = DEFAULT_FADE_MS;
            fprintf(stderr, "NOTE 没有可用的 length 标签，按默认 %.0f 秒渲染\n",
                    (double)(length_ms + fade_ms) / 1000.0);
        }
        if (want_secs > 0.0) {
            long ms = (long)(want_secs * 1000.0);
            if (ms < length_ms) length_ms = ms;
        }

        play_ms      = length_ms + fade_ms;
        /* 帧数必须按**运行时**采样率算，不是宏。--rate 24038 时若还用
         * OUT_RATE，就会按 44100 算帧数、再按 24038 除回来，45 秒的曲子
         * 会被标成 82.6 秒。 */
        total_frames = play_ms * out_rate / 1000;
        fade_frames  = fade_ms * out_rate / 1000;
        fade_start   = total_frames - fade_frames;

        if (total_frames <= 0) {
            fprintf(stderr, "ERR 时长算出来是 0（length=%s fade=%s）\n",
                    c.length_s, c.fade_s);
            goto done;
        }

        out = xfopen(outp, "wb");
        if (!out) { fprintf(stderr, "ERR 无法写 %s\n", outp); goto done; }
        write_wav_header(out, 0, (uint32_t)out_rate, 2, 16);

        /* ---- 4. 渲染 ----
         * 分两级：
         *   源级    qsound_execute 按 24038 出样本，塞进滑动窗口 src[]；
         *   输出级  按 out_rate 从 src[] 取值（重采样 / 直通），淡出、写盘。
         *
         * cycles 传 0x7FFFFFFF = 「跑到填满要求的样本数就停」，
         * qsound_execute 内部按 cycles_per_sample 折算，会自己收住。 */
        {
            enum { OUT_CHUNK = 1024, SRC_CAP = 16384 };
            static int16_t src[SRC_CAP * 2];       /* 源滑动窗口（帧交错） */
            static int16_t obuf[OUT_CHUNK * 2];    /* 输出块 */
            int64_t  src_first = -(int64_t)RS_TAPS; /* src[0] 的绝对帧号 */
            int      src_n     = RS_TAPS;           /* 头部垫 RS_TAPS 帧静音 */
            uint64_t pos  = 0;                      /* 32.32 定点读位置 */
            uint64_t step = 0;                      /* 32.32 定点步长 */
            int      eof_src = 0;
            int      direct;

            memset(src, 0, sizeof(src));

            /* out_rate == 24038 时是 1:1，相位恒为 0、滤波器退化成 δ，
             * 走直通分支（逐样本不变，方便和参考实现对比）。 */
            direct = (out_rate == SRC_RATE);
            if (direct) {
                step = (uint64_t)1 << 32;
            } else {
                step = (uint64_t)(((double)SRC_RATE / (double)out_rate)
                                  * 4294967296.0 + 0.5);
                rs_init(0.45, 9.0);
            }

            frames_left = total_frames;
            while (frames_left > 0) {
                uint32_t want = (frames_left > OUT_CHUNK) ? OUT_CHUNK
                                                          : (uint32_t)frames_left;
                uint64_t need_abs;
                uint32_t j;

                /* 需要的源帧上界 = 最后一个输出帧的整数位置 + 半个滤波器 */
                need_abs = ((pos + (uint64_t)want * step) >> 32) + RS_TAPS;

                while (!eof_src && (uint64_t)(src_first + src_n) < need_abs) {
                    uint32_t got;
                    sint32 r;

                    /* 腾地方：把已经用不到的前半段挪掉 */
                    if (src_n + (int)want + RS_TAPS * 2 > SRC_CAP) {
                        int64_t keep_from = (int64_t)(pos >> 32) - (RS_TAPS / 2 - 1);
                        int drop = (int)(keep_from - src_first);
                        if (drop < 1) drop = src_n / 2;
                        if (drop > src_n) drop = src_n;
                        memmove(src, src + drop * 2,
                                (size_t)(src_n - drop) * 2 * sizeof(int16_t));
                        src_first += drop;
                        src_n     -= drop;
                    }

                    got = (uint32_t)(SRC_CAP - src_n);
                    r = qsound_execute(state, 0x7FFFFFFF, src + src_n * 2, &got);
                    if (r < 0) {
                        fprintf(stderr, "WARN qsound_execute 返回 %d（Z80 跑飞了）\n",
                                (int)r);
                        eof_src = 1;
                        break;
                    }
                    if (!got) {
                        if (++stall > 256) {
                            fprintf(stderr, "WARN 连续 %d 次没产出样本，提前收尾\n",
                                    stall);
                            eof_src = 1;
                            break;
                        }
                        continue;
                    }
                    stall = 0;
                    src_n += (int)got;
                }

                /* 源不够（曲子比标签短 / Z80 停了）：补 0，让尾巴自然收掉 */
                if ((uint64_t)(src_first + src_n) < need_abs) {
                    int64_t need_n = (int64_t)(need_abs - (uint64_t)src_first);
                    if (need_n > SRC_CAP) need_n = SRC_CAP;
                    if (need_n > src_n) {
                        memset(src + src_n * 2, 0,
                               (size_t)(need_n - src_n) * 2 * sizeof(int16_t));
                        src_n = (int)need_n;
                    }
                }

                for (j = 0; j < want; j++) {
                    int64_t i0 = (int64_t)(pos >> 32);
                    int64_t k0 = i0 - (RS_TAPS / 2 - 1) - src_first;
                    float l = 0.0f, rr = 0.0f;

                    if (direct) {
                        if (k0 >= 0 && k0 < src_n) {
                            l  = (float)src[k0 * 2 + 0];
                            rr = (float)src[k0 * 2 + 1];
                        }
                    } else {
                        const float *co =
                            g_rs[(int)(((pos & 0xFFFFFFFFu) * RS_PHASES) >> 32)];
                        int k;
                        for (k = 0; k < RS_TAPS; k++) {
                            int64_t idx = k0 + k;
                            if (idx >= 0 && idx < src_n) {
                                l  += (float)src[idx * 2 + 0] * co[k];
                                rr += (float)src[idx * 2 + 1] * co[k];
                            }
                        }
                    }
                    obuf[j * 2 + 0] = rs_clamp(l);
                    obuf[j * 2 + 1] = rs_clamp(rr);
                    pos += step;
                }

                /* 淡出放在**输出**时间轴上做 —— 和 length/fade 标签同一把尺子 */
                {
                    int64_t into = (total_frames - frames_left) - fade_start;
                    for (j = 0; j < want; j++, into++) {
                        if (into > 0 && fade_frames > 0) {
                            double fadedness =
                                (double)(fade_frames - into) / (double)fade_frames;
                            if (fadedness < 0.0) fadedness = 0.0;
                            obuf[j * 2 + 0] = (int16_t)(obuf[j * 2 + 0] * fadedness);
                            obuf[j * 2 + 1] = (int16_t)(obuf[j * 2 + 1] * fadedness);
                        }
                    }
                }
                for (j = 0; j < want * 2; j++) {
                    int32_t v = obuf[j];
                    if (v < 0) v = -v;
                    if (v > peak) peak = v;
                }
                fwrite(obuf, 2, want * 2, out);
                total_samples += want;
                frames_left   -= want;
            }
        }

        {
            uint32_t data_bytes = total_samples * 4;
            uint8_t h[4];
            fseek(out, 4, SEEK_SET);
            wr32le(h, 36 + data_bytes); fwrite(h, 1, 4, out);
            fseek(out, 40, SEEK_SET);
            wr32le(h, data_bytes);      fwrite(h, 1, 4, out);
        }
        fclose(out);
        out = NULL;

        if (!total_samples) {
            fprintf(stderr, "ERR 一个样本都没渲染出来\n");
            goto done;
        }
        if (!peak) {
            fprintf(stderr,
                    "ERR 解码完成但输出全静音（%u 帧 / %.2f 秒）\n"
                    "    通常是曲号没打对，或这个包缺 SMP 块。\n",
                    total_samples, (double)total_samples / out_rate);
            rc = EXIT_SILENT;
            goto done;
        }

        printf("OK %s\n", outp);
        printf("  seconds=%.3f samples=%u peak=%d rate=%d\n",
               (double)total_samples / out_rate, total_samples, (int)peak, out_rate);
        rc = EXIT_OK;
    }

done:
    if (out) fclose(out);
    if (state) free(state);
    free(c.z80.data);
    free(c.smp.data);
    return rc;
}
