/* vio2wav.c — 2SF / mini2sf（NDS 芯片音源）→ WAV 的命令行解码器
 *
 * 为什么需要它
 * ------------
 * `.mini2sf` 只有几百字节，是**内存快照**：真正的音乐在配套的 `.2sflib`
 * 里，存的是「NDS 声音驱动的 ARM 代码 + 采样 + 寄存器现场」。要出声必须真的
 * 跑 ARM7/ARM9 和 DeSmuME 的 SPU。解析器路线走不通。
 *
 * 本工具把三件事拼起来：
 *
 *   psflib   （MIT）        解析 XSF 容器：zlib 解压、`_lib` 链、tags
 *   vio2sf   （GPLv2+）     DeSmuME 抽出来的最小 NDS 核（ARM + SPU）
 *   zlib     （zlib 许可）  psflib 只用它一个 uncompress()
 *
 * 为什么**不要** BIOS
 * -------------------
 * vio2sf 的 `bios.c` 是 DeSmuME 的 HLE 实现（自己算 BIOS 行为，不读文件），
 * 所以和 `usf2wav` 一样，本工具**不需要任何外部文件**。
 *
 * 2SF 的装载为什么这么绕
 * ----------------------
 * `.2sf` 的 exe 段不是一整块 ROM，而是一串「映射表项」：
 *
 *     u32 xofs   目标地址（在模拟 ROM 里的偏移）
 *     u32 xsize  这一块的长度
 *     ...        紧跟着 xsize 字节的数据
 *
 * 而 reserved 段里是若干个「SAVE」块（`0x45564153` == "SAVE"），每块是
 * zlib 压缩过的**内存快照**，解出来之后同样按「xofs + xsize + 数据」拼。
 * 多个段（主文件 + `.2sflib`）会依次回调，所以要**累加拼接到同一块内存**，
 * 缺的地方补 0。这套逻辑照 kode54 `audacious-xsf` 的 `twosf_loader` 实现。
 *
 * 为什么要 1 字节对齐向上取整
 * ---------------------------
 * 原实现把 ROM 尺寸向上取整到 2 的幂再 +1（`rsize |= rsize >> 1 …` 那一串），
 * 这是 DeSmuME 的 `rom_mask` 寻址要求 —— ROM 大小必须是 2 的幂，掩码才能
 * 直接用位与。少这一步，个别游戏会在 MMU 里跑飞。
 *
 * `length` / `fade` 标签
 * ----------------------
 * 和 USF 一样，时长**只来自标签**。没有 `length` 时兜底 DEFAULT_SECS 秒。
 *
 * 用法
 * ----
 *   vio2wav <input.mini2sf> <output.wav> [秒数] [--no-fade]
 *   vio2wav --info <input.mini2sf>
 *
 * `--no-fade` 关掉淡出。**逐样本比对时必须加**：淡出是乘上去的斜坡，
 * 参考实现如果没做淡出，差的就是那条曲线而不是解码结果。
 *
 * 退出码
 * ------
 *   0  成功
 *   1  参数错误
 *   2  psf_load 失败（不是有效的 2SF，或者 _lib 找不到）
 *   3  渲染出 0 个样本
 *   4  内存分配失败
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
#define OUT_BITS    16
#define BUF_FRAMES  1024        /* 每次渲染的帧数 */
#define DEFAULT_SECS 180.0      /* 没有 length 标签时的兜底时长 */

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

/*: `1:23:45.678` / `83.5` / `1:23` → 毫秒。非法返回 -1。
 *
 * 抄自 lazyusf2 的 `test/dumpresampled.c`，但把它的 `BORK_TIME` 哨兵换成 -1，
 * 免得一个坏标签把时长算成天文数字。 */
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
/*  2SF 装载：把若干「映射表项」拼成一块模拟 ROM / 内存快照             */
/* ------------------------------------------------------------------ */

typedef struct {
    uint8_t *rom;
    size_t   rom_size;
    uint8_t *snap;          /* 内存快照（reserved 段里的 SAVE 块） */
    size_t   snap_size;

    /* 标签 */
    char title[256];
    char game[256];
    char length_s[64];
    char fade_s[64];

    /* 2sf 私有标签：调同步与降频，缺省值必须是 0 / -1 */
    int initial_frames;
    int sync_type;
    int clockdown;
    int arm9_clockdown_level;
    int arm7_clockdown_level;

    int n_section;          /* 一共喂了几段（排错用） */
} loadctx;

/*: 把一块 `xofs + xsize + 数据` 拼进目标缓冲。
 *
 * `is_snap` 区分「拼进 ROM」还是「拼进快照」。
 * 返回 0 成功，-1 失败（调用方负责报错并中止）。 */
static int put_map(loadctx *c, int is_snap, const uint8_t *udata, unsigned usize)
{
    uint8_t **dst_p;
    size_t   *size_p;
    uint8_t  *iptr;
    size_t    isize;
    unsigned  xsize, xofs;

    if (usize < 8) return -1;

    xsize = get_le32(udata + 4);
    xofs  = get_le32(udata + 0);

    if (is_snap) { dst_p = &c->snap;     size_p = &c->snap_size; }
    else         { dst_p = &c->rom;      size_p = &c->rom_size;  }

    iptr  = *dst_p;
    isize = *size_p;
    /* 先「摘下来」，后面 realloc 失败时不会留下悬空指针 */
    *dst_p = NULL;
    *size_p = 0;

    if (!iptr) {
        size_t rsize = (size_t)xofs + xsize;
        if (!is_snap) {
            /* ROM 尺寸向上取整到 2 的幂 —— DeSmuME 用 rom_mask 直接位与寻址 */
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        iptr = (uint8_t *)malloc(rsize + 10);
        if (!iptr) return -1;
        memset(iptr, 0, rsize + 10);
        isize = rsize;
    } else if (isize < (size_t)xofs + xsize) {
        size_t rsize = (size_t)xofs + xsize;
        uint8_t *xptr;
        if (!is_snap) {
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        xptr = (uint8_t *)realloc(iptr, (size_t)xofs + rsize + 10);
        if (!xptr) { free(iptr); return -1; }
        iptr = xptr;
        isize = rsize;
    }

    memcpy(iptr + xofs, udata + 8, xsize);
    *dst_p = iptr;
    *size_p = isize;
    return 0;
}

/*: SAVE 块是 zlib 压的，先解压再 put_map。
 *
 * 解压尺寸未知（头 8 字节里带 xsize，但整体可能不止），所以按
 * 「失败就翻倍重试」的循环来 —— 照原实现。 */
static int put_mapz(loadctx *c, int is_snap, const uint8_t *zdata,
                    unsigned zsize, unsigned zcrc)
{
    int ret;
    int zerr;
    uLongf usize = 8;
    uLongf rsize = usize;
    uint8_t *udata;
    uint8_t *rdata;

    (void)zcrc;     /* 原实现也把 CRC 校验用 `if (0)` 关掉了 */

    udata = (uint8_t *)malloc(usize);
    if (!udata) return -1;

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
            } else {
                rsize = usize;
            }
        } else {
            rsize += rsize;
            usize = rsize;
        }
        rdata = (uint8_t *)realloc(udata, usize);
        if (!rdata) { free(udata); return -1; }
        udata = rdata;
    }

    rdata = (uint8_t *)realloc(udata, usize);
    if (!rdata) { free(udata); return -1; }

    ret = put_map(c, is_snap, rdata, (unsigned)usize);
    free(rdata);
    return ret;
}

/*: 每个 `_lib` 段/主文件都会被回调一次。 */
static int load_cb(void *ctx, const uint8_t *exe, size_t exe_size,
                   const uint8_t *reserved, size_t reserved_size)
{
    loadctx *c = (loadctx *)ctx;

    /* ---- exe 段：映射表项，拼进模拟 ROM ---- */
    if (exe_size >= 8) {
        if (put_map(c, 0, exe, (unsigned)exe_size) != 0) {
            fprintf(stderr, "ERR 2SF exe 段映射失败（%zu 字节）\n", exe_size);
            return -1;
        }
    }

    /* ---- reserved 段：一串 SAVE 块，拼进内存快照 ---- */
    if (reserved_size) {
        size_t pos = 0;
        if (reserved_size < 16) return -1;
        while (pos + 12 < reserved_size) {
            unsigned save_size = get_le32(reserved + pos + 4);
            unsigned save_crc  = get_le32(reserved + pos + 8);
            if (get_le32(reserved + pos + 0) == 0x45564153u) {   /* "SAVE" */
                if (pos + 12 + save_size > reserved_size) {
                    fprintf(stderr, "ERR SAVE 块越界（%u 字节）\n", save_size);
                    return -1;
                }
                if (put_mapz(c, 1, reserved + pos + 12, save_size, save_crc) != 0) {
                    fprintf(stderr, "ERR SAVE 块解压/映射失败\n");
                    return -1;
                }
            }
            pos += 12 + save_size;
        }
    }

    c->n_section++;
    return 0;
}

static int info_cb(void *ctx, const char *name, const char *value)
{
    loadctx *c = (loadctx *)ctx;
    char *end;

    if (!name || !value) return 0;

    if (!strcmp(name, "title"))  { snprintf(c->title,    sizeof(c->title),    "%s", value); return 0; }
    if (!strcmp(name, "game"))   { snprintf(c->game,     sizeof(c->game),     "%s", value); return 0; }
    if (!strcmp(name, "length")) { snprintf(c->length_s, sizeof(c->length_s), "%s", value); return 0; }
    if (!strcmp(name, "fade"))   { snprintf(c->fade_s,   sizeof(c->fade_s),   "%s", value); return 0; }

    if (!strcmp(name, "_frames"))       c->initial_frames = (int)strtol(value, &end, 10);
    else if (!strcmp(name, "_clockdown"))      c->clockdown = (int)strtol(value, &end, 10);
    else if (!strcmp(name, "_vio2sf_sync_type"))
        c->sync_type = (int)strtol(value, &end, 10);
    else if (!strcmp(name, "_vio2sf_arm9_clockdown_level"))
        c->arm9_clockdown_level = (int)strtol(value, &end, 10);
    else if (!strcmp(name, "_vio2sf_arm7_clockdown_level"))
        c->arm7_clockdown_level = (int)strtol(value, &end, 10);

    return 0;
}

/* ---- psflib 的文件回调（Windows 上直接用 stdio） ---- */

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
    "\\/",          /* path_separators */
    NULL,           /* context */
    f_open, f_read, f_seek, f_close, f_tell
};

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

static void usage(void)
{
    fprintf(stderr,
        "用法:\n"
        "  vio2wav <input.mini2sf> <output.wav> [秒数] [--no-fade]\n"
        "  vio2wav --info <input.mini2sf>\n"
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
    NDS_state *nds = NULL;
    int rval;
    FILE *out = NULL;
    int16_t buf[BUF_FRAMES * OUT_CH];
    int64_t frames_left, total_frames = 0;
    uint32_t data_bytes = 0;
    long length_ms, fade_ms;

    /* ---- 参数 ---- */
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
    c.initial_frames = -1;      /* 原实现的缺省值就是 -1 */

    /* ---- 1. 建核 ---- */
    nds = (NDS_state *)calloc(1, sizeof(*nds));
    if (!nds) {
        fprintf(stderr, "ERR 分配 NDS_state 失败\n");
        return EXIT_NOMEM;
    }
    if (state_init(nds) != 0) {
        fprintf(stderr, "ERR state_init 失败\n");
        free(nds);
        return EXIT_NOMEM;
    }

    /* ---- 2. 解析 2SF 容器（版本字节 0x24），递归跟 `_lib` 链 ---- */
    rval = psf_load(in_path, &g_callbacks, 0x24, load_cb, &c,
                    info_cb, &c, 1, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr,
                "ERR psf_load 失败 (%d)：不是有效的 2SF，"
                "或者配套的 .2sflib 找不到\n", rval);
        state_deinit(nds);
        free(nds);
        return EXIT_PARSE;
    }
    if (!c.rom && !c.snap) {
        fprintf(stderr, "ERR 2SF 里既没有 ROM 数据也没有内存快照，无法出声\n");
        state_deinit(nds);
        free(nds);
        return EXIT_PARSE;
    }
    /* ⚠ 没有 SAVE 块是**正常情况**，不是错误。
     *
     * `.mini2sf` 分两类：
     *   a) 带内存快照 —— exe 只是「把 ROM 打几个补丁」，reserved 里有 SAVE 块
     *   b) **不带快照** —— exe 打的是 ROM 补丁，音乐直接**烧在 ROM 里**
     *      （常见于「自制驱动 + 单曲 ROM」的 joshw 包，标签里会写
     *      "uses XXX driver hacked by YYY"）。这类文件从复位开始跑就能出声，
     *      靠 `_frames` 标签跳过前 N 帧让驱动起来。
     *
     * 第一版我把「没有快照」当成了致命错误，于是 110 个文件全部报错 ——
     * 这正是「先看真实样本再定判据」的价值。 */

    /* ---- 3. 喂给核 ----
     * 顺序不能换：先 setrom，再 loadstate（快照里存的是 ROM 已就位时的现场）。 */
    if (c.rom) state_setrom(nds, c.rom, (u32)c.rom_size, 0);

    if (!c.arm7_clockdown_level) c.arm7_clockdown_level = c.clockdown;
    if (!c.arm9_clockdown_level) c.arm9_clockdown_level = c.clockdown;

    nds->dwInterpolation = 1;       /* 线性插值，和原实现一致 */
    nds->dwChannelMute  = 0;
    nds->initial_frames = c.initial_frames;
    nds->sync_type      = c.sync_type;
    nds->arm7_clockdown_level = c.arm7_clockdown_level;
    nds->arm9_clockdown_level = c.arm9_clockdown_level;

    state_loadstate(nds, c.snap, (u32)c.snap_size);

    length_ms = parse_time_ms(c.length_s);
    fade_ms   = parse_time_ms(c.fade_s);
    if (fade_ms < 0) fade_ms = 0;

    if (info_only) {
        printf("version=0x24\n");
        printf("title=%s\n", c.title);
        printf("game=%s\n", c.game);
        printf("length=%s\n", c.length_s[0] ? c.length_s : "(无)");
        printf("fade=%s\n", c.fade_s[0] ? c.fade_s : "(无)");
        printf("sections=%d\n", c.n_section);
        printf("rom_size=%zu\n", c.rom_size);
        printf("snap_size=%zu\n", c.snap_size);
        printf("length_ms=%ld\n", length_ms);
        printf("fade_ms=%ld\n", fade_ms);
        state_deinit(nds);
        free(nds);
        free(c.rom);
        free(c.snap);
        return EXIT_OK;
    }

    /* ---- 4. 算总帧数 ---- */
    if (length_ms < 0) {
        length_ms = (long)(DEFAULT_SECS * 1000.0);
        fprintf(stderr,
                "NOTE 没有可用的 length 标签，按默认 %.0f 秒渲染\n",
                DEFAULT_SECS);
    }
    if (want_secs > 0.0) {
        long ms = (long)(want_secs * 1000.0);
        if (ms < length_ms) length_ms = ms;
    }
    if (no_fade) fade_ms = 0;

    /* 帧数换算用整数 `ms * 441 / 10`，和 USF 那边保持同一套口径。
       ⚠ 总长是 **length + fade**，不是 length —— 淡出段是**追加**在后面的。 */
    {
        long play_ms = length_ms + fade_ms;
        total_frames = (int64_t)play_ms * 441 / 10;
    }

    {
        int64_t fade_frames = (int64_t)fade_ms * 441 / 10;
        int64_t fade_start;

        /* ---- 5. 开文件，先占位头 ---- */
        out = fopen(out_path, "wb");
        if (!out) {
            fprintf(stderr, "ERR 无法写入 %s\n", out_path);
            state_deinit(nds);
            free(nds);
            free(c.rom);
            free(c.snap);
            return EXIT_USAGE;
        }
        if (write_wav_header(out, 0, OUT_RATE, OUT_CH, OUT_BITS) != 0) {
            fprintf(stderr, "ERR 写 WAV 头失败\n");
            fclose(out);
            state_deinit(nds);
            free(nds);
            free(c.rom);
            free(c.snap);
            return EXIT_USAGE;
        }

        /* ---- 6. 渲染 ---- */
        fade_start = total_frames - fade_frames;
        frames_left = total_frames;
        while (frames_left > 0) {
            int32_t n = (frames_left > BUF_FRAMES)
                        ? BUF_FRAMES : (int32_t)frames_left;
            int32_t j;
            int samples_into_fade;

            state_render(nds, buf, (unsigned int)n);

            /* 淡出：**必须**照 plugin.cc 的浮点写法，不能用整数除法。
             *
             * 这不是「差不多就行」的地方。实测反例：
             *   length=3:10 fade=10，总长 8820000 帧，fade 段 441000 帧。
             *   在 left=119700 处，原始样本 2800：
             *     整数  2800 * 119700 / 441000 = 760    ← 精确
             *     浮点  2800 * (119700.0/441000.0) = 759.9999999999999 → 759
             *   差 1 个 LSB。听不出来，但「最大差 == 0」的判据过不去，
             *   而且它说明我和上游不是同一套算法。
             * 参照物是上游，就跟上游走。 */
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

        /* ---- 7. 回填两个长度字段（偏移 4 和 40，别写 0） ---- */
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
            state_deinit(nds);
            free(nds);
            free(c.rom);
            free(c.snap);
            return EXIT_EMPTY;
        }
    }

    if (getenv("VIO2WAV_VERBOSE"))
        fprintf(stderr, "OK %d 段 %lld 帧 %.2f 秒\n",
                c.n_section, (long long)(data_bytes / (OUT_CH * 2)),
                (double)(data_bytes / (OUT_CH * 2)) / OUT_RATE);

    state_deinit(nds);
    free(nds);
    free(c.rom);
    free(c.snap);
    return EXIT_OK;
}
