/* psf2wav.c — PSF / minipsf / PSF2 / minipsf2（PS1/PS2 芯片音源）→ WAV 的命令行解码器
 *
 * 为什么需要它
 * ------------
 * PSF 里存的不是波形，而是一份 **PS1 内存快照 + 一段可执行的 PS-X EXE**。
 * 要出声必须真的模拟 R3000A CPU 和 SPU 的 24 个声道 —— 这不是「写个解析器」
 * 能解决的。本工具把三件事拼起来：
 *
 *   psflib              （MIT）           解析 PSF 容器：zlib 解压、_lib 链、tags
 *   psf2fs              （MIT）           解析 PSF2 的 reserved 虚拟文件系统
 *   Highly Experimental （PSXCore）       模拟 R3000A + SPU（PS1）/ IOP + SPU2（PS2）
 *   zlib                （zlib 许可）       psflib 只用它一个 uncompress()
 *
 * 两种版本、两条路径（照抄 kode54 audacious-xsf plugin.cc）：
 *
 *   version 1（PS1，.psf/.minipsf）
 *     程序是 PS-X EXE，直接 psx_upload_psxexe 进 IOP RAM；采样率 44100。
 *
 *   version 2（PS2，.psf2/.minipsf2）
 *     程序和数据都在 reserved section 打包的虚拟文件系统里，由
 *     psf2fs 解成文件树，再经 psx_set_readfile 挂成 VFS —— IOP 里的
 *     cdvdnul.irx 之类的模块通过 emucall vopen/vread 去读文件；
 *     采样率 48000（IOP 36.864MHz / 768 cycles_per_sample）。
 *
 * BIOS：**必须由用户提供**，这是绕不过去的
 * ------------------------------------------
 * HE 的 psx_init() 硬性要求先注入 BIOS，而且 bios_getenv() 会在 BIOS 偏移
 * 0x80 处找 "Highly Experimental" 这段 banner 才肯返回环境变量 —— 也就是说
 * **一份干净的 PS1 BIOS 直接用是不行的**。
 *
 * HE 的标准做法是用自带的 `mkhebios` 把一份 **4MB 的 PS2 BIOS** 转成
 * **512KB 的 HE BIOS**：
 *
 *     mkhebios_create(ps2_bios, &size)     size: 0x400000 → 0x80000
 *
 * 产出的 HE BIOS 里除了 Sony 的 IOP 内核，还打进了 HE 自己的覆盖层
 * （`mkhebios_overlays.h`，4388 行 MIPS 机器码）。这一层补上的，恰恰是
 * 「手写 HLE 桩」永远补不齐的那一整套环境：
 *
 *   * 低 RAM 的 A0 / B0 / C0 三张调用表桩
 *   * 异常向量 0x80000080 上的中断派发
 *   * 事件系统（OpenEvent / WaitEvent / TestEvent / DeliverEvent）
 *   * 定时器 2 的 VSync 中断
 *   * banner 与环境变量区（ps1preboot / ps2preboot）
 *
 * 实测对照（SYS_ME 这个 PSF-o-Cycle 包，同一首）：
 *
 *   | 方案            | SPU 寄存器写入 | key-on 非零 | 输出         |
 *   |-----------------|---------------|------------|--------------|
 *   | 自造 HLE BIOS   | 644 次        | 0 次       | 全 0（静音） |
 *   | 真 BIOS + mkhebios | 4832 次    | 32 次      | peak 16691   |
 *
 * 只靠 HLE 桩时，驱动初始化完（采样上传 177664 字节、主音量拉满）就执行到
 * 0x80100414 的 `beq $0,$0,-1`（停机自旋）不动了 —— 它在等 VSync 中断回调，
 * 而我们没有中断系统。所以**没有 BIOS 就不该硬撑**，直接报错更诚实。
 *
 * 许可
 * ----
 * **本工具不分发任何 BIOS**。运行时由用户指向他自己机器上的文件，
 * 和 foobar2000 + foo_psf 的要求完全一样。
 *
 * 用法
 * ----
 *   psf2wav <input.psf> <output.wav> [秒数] --bios <BIOS>
 *   psf2wav --info <input.psf>
 *   psf2wav --check-bios <BIOS>
 *
 * BIOS 也可以用环境变量 PSF2WAV_BIOS 指定。
 *
 * 退出码
 * ------
 *   0  成功（且确实有声音）
 *   1  一般错误（参数 / 文件 / 解析 / 初始化）
 *   2  用法错误
 *   3  解码跑完了但输出全静音（PSF 不兼容，或 BIOS 不对）
 *   4  BIOS 缺失或不可用
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "psflib.h"
#include "psf2fs.h"
#include "psx.h"
#include "bios.h"
#include "iop.h"
#include "r3000.h"
#include "mkhebios.h"

#define EXIT_OK        0
#define EXIT_FAIL      1
#define EXIT_USAGE     2
#define EXIT_SILENT    3
#define EXIT_NO_BIOS   4
#define EXIT_PS2       5

/* ------------------------------------------------------------------ */
/*  小工具                                                             */
/* ------------------------------------------------------------------ */

static uint32_t rd32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void wr32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static void wr16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

/* ------------------------------------------------------------------ */
/*  BIOS                                                               */
/* ------------------------------------------------------------------ */

/*: HE BIOS 的固定大小（= 4MB / 8） */
#define HE_BIOS_SIZE 0x80000u
/*: 一份 PS2 BIOS 的大小 */
#define PS2_BIOS_SIZE 0x400000u

/* 读整个文件；失败返回 NULL */
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

/* 把用户给的 BIOS 变成可用的 HE BIOS（512KB）。
 *   * 4MB  → 认定是 PS2 BIOS，跑 mkhebios 转换
 *   * 512KB→ 认定已经是 HE BIOS，原样使用
 * 返回 malloc 的缓冲，调用方负责 free；失败返回 NULL。 */
static uint8_t *load_he_bios(const char *path, uint32_t *size_out)
{
    long n = 0;
    uint8_t *buf = slurp(path, &n);

    if (!buf) {
        fprintf(stderr, "ERR 读不了 BIOS 文件：%s\n", path);
        return NULL;
    }
    if (n == (long)PS2_BIOS_SIZE) {
        int osz = (int)n;
        void *he = mkhebios_create(buf, &osz);
        free(buf);
        if (!he) {
            fprintf(stderr, "ERR mkhebios 转换失败（这份 4MB 文件不是有效的 PS2 BIOS）\n");
            return NULL;
        }
        *size_out = (uint32_t)osz;
        return (uint8_t *)he;
    }
    if (n == (long)HE_BIOS_SIZE) {
        *size_out = HE_BIOS_SIZE;
        return buf;
    }
    fprintf(stderr,
            "ERR BIOS 大小 %ld 字节不对。需要：\n"
            "      * 4MB（4194304）的 PS2 BIOS，例如 scph39001.bin / SCPH30004R.bin\n"
            "      * 或 512KB（524288）的 HE BIOS\n", n);
    free(buf);
    return NULL;
}

/* ------------------------------------------------------------------ */
/*  psflib 加载上下文                                                  */
/* ------------------------------------------------------------------ */

/*: 一个程序块。PSF 的 program data **本身就是 PS-X EXE 格式**：
 *  实测 SYS_ME.psflib 解压后开头就是 "PS-X EXE"，
 *  0x10=PC / 0x18=text_start / 0x1C=text_size / 0x30=SP。
 *  所以不需要拼假 header，直接丢给 psx_upload_psxexe 即可。 */
typedef struct {
    uint8_t *data;
    size_t   len;
} blk_t;

typedef struct {
    blk_t   *blk;          /* psflib 回调顺序：最深层 _lib 先 → 主文件 → _lib2 */
    int      n_blk;
    int      cap_blk;
    char     title[256];
    char     game[256];
    char     length_s[64];
    char     fade_s[64];
    int      version;
} loadctx;

static int load_cb(void *ctx, const uint8_t *exe, size_t exe_size,
                   const uint8_t *reserved, size_t reserved_size)
{
    loadctx *c = (loadctx *)ctx;
    (void)reserved;
    (void)reserved_size;

    if (!exe_size) return 0;
    if (exe_size < 0x801) return 0;              /* 太小，不是合法 EXE */
    if (memcmp(exe, "PS-X EXE", 8)) return 0;    /* 不是 PS-X EXE，跳过 */

    if (c->n_blk == c->cap_blk) {
        int cap = c->cap_blk ? c->cap_blk * 2 : 8;
        blk_t *p = (blk_t *)realloc(c->blk, (size_t)cap * sizeof(blk_t));
        if (!p) return -1;
        c->blk = p;
        c->cap_blk = cap;
    }
    c->blk[c->n_blk].data = (uint8_t *)malloc(exe_size);
    if (!c->blk[c->n_blk].data) return -1;
    memcpy(c->blk[c->n_blk].data, exe, exe_size);
    c->blk[c->n_blk].len = exe_size;
    c->n_blk++;
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

/* psflib 内部状态日志 → stderr（调试用；正常解码时也打印，便于定位问题） */
static void status_cb(void *ctx, const char *msg)
{
    (void)ctx;
    if (msg) fputs(msg, stderr);
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

/* ---- PSF2 的 VFS 桥：IOP 里读文件 → psf2fs（reserved 解出的文件树） ----
 *
 * 照抄 kode54 plugin.cc 的 virtual_readfile()：PSF2 的程序和数据都在
 * reserved section 打包的虚拟文件系统里（IOP 用 cdvdnul.irx 之类通过
 * emucall vopen/vread 访问），HE 的 psx_set_readfile 把这个回调挂成
 * VFS 的底层读取器，IOP 请求文件时就到这里按名字/偏移去解 zlib 块。 */
static int EMU_CALL virtual_readfile(void *context, const char *path,
                                     int offset, char *buffer, int length)
{
    return psf2fs_virtual_readfile(context, path, offset, buffer, length);
}

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

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

/* 采样率按版本分：照抄 kode54 plugin.cc 的 get_srate()。
 *   PSF (version 1)  → 44100
 *   PSF2 (version 2) → 48000（IOP 36.864MHz / 768 cycles_per_sample） */
#define OUT_RATE_PS1   44100
#define OUT_RATE_PS2   48000
#define CPU_HZ         33868800
/* 一次跑 1/60 秒：PS1 的 NTSC 帧率 */
#define CYCLES_PER_FRAME  (CPU_HZ / 60)

/*: `1:18.6` / `83.5` / `1:23` → 毫秒。非法返回 -1。
 *
 * 抄自 lazyusf2 的 `test/dumpresampled.c`，和 usf2wav / vio2wav / ssf2wav /
 * gsf2wav 是同一份。⚠ 别凭印象重写成「分:秒.百分秒」—— 上游是用 `strtod`
 * 读最后一节，`18.6` 就是 18600 毫秒。 */
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
        "psf2wav — PSF / minipsf（PS1）→ WAV\n"
        "\n"
        "用法:\n"
        "  psf2wav <input.psf> <output.wav> [秒数] --bios <BIOS>\n"
        "  psf2wav --info <input.psf>\n"
        "  psf2wav --check-bios <BIOS>\n"
        "\n"
        "  --bios <BIOS>   4MB 的 PS2 BIOS（如 scph39001.bin），\n"
        "                  或 512KB 的 HE BIOS；也可用环境变量 PSF2WAV_BIOS\n"
        "  秒数            不写就用 PSF 自带的 length 标签，再不行 60 秒\n");
}

int main(int argc, char **argv)
{
    loadctx c;
    uint8_t *bios = NULL;
    uint32_t bios_size = 0;
    const char *bios_path = NULL;
    void *state = NULL;
    void *emulator_extra = NULL;   /* PSF2 的 psf2fs（version 2 专用） */
    FILE *out = NULL;
    int rc = EXIT_FAIL;
    int info_only = 0;
    int check_bios = 0;
    double want_secs = 0.0;
    int i;
    int npos = 0;
    const char *pos[4];

    memset(&c, 0, sizeof(c));

    /* ---- 扫参数：--info / --check-bios / --bios <path>，其余按位置取 ---- */
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) {
            info_only = 1;
        } else if (!strcmp(argv[i], "--check-bios")) {
            check_bios = 1;
        } else if (!strcmp(argv[i], "--bios") && i + 1 < argc) {
            bios_path = argv[++i];
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage();
            return EXIT_USAGE;
        } else if (npos < 4) {
            pos[npos++] = argv[i];
        }
    }
    if (!bios_path || !bios_path[0]) bios_path = getenv("PSF2WAV_BIOS");
    if (bios_path && !bios_path[0]) bios_path = NULL;

    /* ---- --check-bios：只验证 BIOS 可用性 ---- */
    if (check_bios) {
        const char *p = pos[0] ? pos[0] : bios_path;
        uint32_t sz = 0;
        uint8_t *b;
        if (!p) { usage(); return EXIT_NO_BIOS; }
        b = load_he_bios(p, &sz);
        if (!b) return EXIT_NO_BIOS;
        free(b);
        printf("BIOS_OK %s size=%u\n", p, (unsigned)sz);
        return EXIT_OK;
    }

    if (npos < (info_only ? 1 : 2)) {
        usage();
        return EXIT_USAGE;
    }

    {
        const char *in   = pos[0];
        const char *outp = info_only ? NULL : pos[1];
        if (!info_only && npos >= 3) want_secs = atof(pos[2]);

        /* ---- 1. 解析 PSF ---- */
        c.version = psf_load(in, &g_callbacks, 0, load_cb, &c,
                             info_cb, &c, 1, status_cb, NULL);
        if (c.version < 0) {
            fprintf(stderr, "ERR psf_load 失败 (%d)：不是有效的 PSF\n", c.version);
            goto done;
        }
        /* PSF (version 1) 必须至少有一个 PS-X EXE 块；PSF2 (version 2) 的
         * exe 段固定为空（程序在 reserved 打包的虚拟文件系统里），所以
         * n_blk == 0 是正常情况，不能在这里拦下来。 */
        if (!c.n_blk && c.version == 1) {
            fprintf(stderr, "ERR PSF 里没有可用的 PS-X EXE 块\n");
            goto done;
        }

        if (info_only) {
            printf("version=%d\n", c.version);
            printf("title=%s\n", c.title);
            printf("game=%s\n", c.game);
            printf("length=%s\n", c.length_s);
            printf("fade=%s\n", c.fade_s);
            printf("blocks=%d\n", c.n_blk);
            for (i = 0; i < c.n_blk; i++) {
                const uint8_t *h = c.blk[i].data;
                printf("  [%d] %7zu B  pc=0x%08X  start=0x%08X  size=%u\n",
                       i, c.blk[i].len,
                       rd32le(h + 0x10), rd32le(h + 0x18), rd32le(h + 0x1C));
            }
            rc = EXIT_OK;
            goto done;
        }

        /* PSF (version 1) 与 PSF2 (version 2) 是两套装载路径：
         *   version 1 → 直接 upload PS-X EXE 块；
         *   version 2 → 程序在 reserved 打包的虚拟文件系统里，
         *               用 psf2fs 收集 + psx_set_readfile 挂 VFS。 */

        /* ---- 2. BIOS（必需） ---- */
        if (!bios_path) {
            fprintf(stderr,
                    "ERR 没有指定 BIOS，无法解码 PSF。\n"
                    "    PSF 需要一份 PS2 BIOS（4MB）或 HE BIOS（512KB）。\n"
                    "    用 --bios <路径> 或环境变量 PSF2WAV_BIOS 指定。\n");
            rc = EXIT_NO_BIOS;
            goto done;
        }
        bios = load_he_bios(bios_path, &bios_size);
        if (!bios) { rc = EXIT_NO_BIOS; goto done; }
        bios_set_image(bios, bios_size);

        if (psx_init() != 0) {
            fprintf(stderr, "ERR psx_init 失败\n");
            goto done;
        }

        {
            uint32_t ss = psx_get_state_size((uint8_t)c.version);
            if (!ss) { fprintf(stderr, "ERR psx_get_state_size 返回 0\n"); goto done; }
            state = calloc(1, ss);
        }
        if (!state) { fprintf(stderr, "ERR 分配模拟器状态失败\n"); goto done; }
        psx_clear_state(state, (uint8_t)c.version);

        if (c.version == 2) {
            /* ---- PSF2：收集 reserved 文件系统 + 挂 VFS ----
             *
             * 照抄 kode54 plugin.cc 的 0x02 分支：
             *   psf2fs_create() → psf_load_vfs(uri, 2, psf2fs_load_callback,
             *                                 fs, ...) → psx_set_readfile()。
             *
             * psf2fs_load_callback 忽略 exe 段（PSF2 的程序不是 PS-X EXE，
             * 而是 IOP 启动时要读的 ELF/irx），只把 reserved section 解成
             * 一个虚拟文件系统；其余一切（入口 PC、loader 行为）都由
             * HE 的 PS2 preboot 从 VFS 里读文件完成。 */
            void *fs = psf2fs_create();
            if (!fs) { fprintf(stderr, "ERR psf2fs_create 失败\n"); goto done; }

            {
                /* 重新装载：psf_load 会把原始 reserved 数据再整个过一遍。
                 * 注意此时 c.blk[] 里存的是 exe 段（PSF2 里基本是空），
                 * psf2fs_load_callback 只取 reserved。 */
                int v = psf_load(in, &g_callbacks, 2, psf2fs_load_callback, fs,
                                 info_cb, &c, 1, status_cb, NULL);
                if (v != 2) {
                    fprintf(stderr, "ERR PSF2 装载失败（psf_load=%d）\n", v);
                    psf2fs_delete(fs);
                    rc = EXIT_FAIL;
                    goto done;
                }
            }

            psx_set_readfile(state, virtual_readfile, fs);
            emulator_extra = fs;   /* 渲染完后统一 psf2fs_delete */
        } else {
            /* ---- PSF（version 1）：依次 upload 每个 PS-X EXE 块 ----
               psflib 的回调顺序是「最深层 _lib 先 → 主文件 → _lib2」。 */
            for (i = 0; i < c.n_blk; i++) {
                sint32 r = psx_upload_psxexe(state, c.blk[i].data,
                                             (uint32_t)c.blk[i].len);
                if (r != 0) {
                    fprintf(stderr, "ERR 第 %d 块 upload 失败 (%d)\n", i, (int)r);
                    goto done;
                }
            }

            /* ---- 3b. 把入口点拨回**第 0 块**（最深层 lib）----
             *
             * ⚠ 这一步不能省。psx_upload_psxexe 每次都顺手把 PC 改成该 EXE 头里的
             *   init_pc，于是最后一块（主 .psf）的 PC 会盖掉前面所有块。
             *
             * 但「PSF-o-Cycle」这类 ripset 的分工是：
             *   * `.psflib`  = 整个系统镜像（实测 1705984 B 全 RAM 快照），
             *                  它的 init_pc 才是**真正的入口**；
             *   * 单曲 `.psf` = 一个 28 字节的**参数表**（不是代码！）。
             *
             * 实测 SYS_ME_00.psf 的 program data：
             *   0,0,0,1,1,0,10     ← 只有 word[5] 是曲号（_01 那首是 1）
             * 它的 EXE 头写着 init_pc=0x80101000，照单全收就会去执行这堆参数
             * → `[cpu] 非法指令 @ PC=0x8010100C`。
             *
             * 而 psflib 的 init_pc=0x80024058 处是一条跳板：
             *   0x80024058: 0804001F   j 0x8010007C
             * 0x8010007C 才是驱动入口（读参数表 → 调真正的序列器）。
             *
             * 所以：照旧把所有块 upload 进去（参数表得进 RAM），
             * 然后只把 PC / SP 拨回第 0 块的值。
             * 单文件 minipsf（没有 _lib，只有 1 块）时这一步是空操作。 */
            {
                void  *r3  = iop_get_r3000_state(psx_get_iop_state(state));
                uint32_t pc0 = rd32le(c.blk[0].data + 0x10);
                uint32_t sp0 = rd32le(c.blk[0].data + 0x30);
                r3000_setreg(r3, R3000_REG_PC    , pc0);
                r3000_setreg(r3, R3000_REG_GEN+29, sp0);
            }
        }

        /* ---- 4. 渲染 ---- */
        {
            const uint32_t out_rate = (c.version == 2) ? OUT_RATE_PS2 : OUT_RATE_PS1;
            /* ⚠ 必须和 plugin.cc 一致：MIN_BUFFER_SIZE=576（plugin.cc:59）。
               上游对 PS1/PS2 都按这个粒度填样本。 */
            enum { MIN_BUFFER_SIZE = 576 };
            int16_t buf[MIN_BUFFER_SIZE * 2];
            uint32_t total_samples = 0;
            int32_t  peak = 0;
            int64_t length_ms, fade_ms, play_ms;
            int64_t total_frames, fade_frames, fade_start;
            int64_t frames_left;
            int silent_frames = 0;

            /* ---- 4a. 算总样本数（照抄 plugin.cc play()） ----
               stream_samples_amount = (length_ms + fade_ms) * rate / 1000
               fade_samples         = fade_ms * rate / 1000
               没有 length 标签时兜底 170 + 10 = 180 秒（settings 默认值）。 */
            length_ms = parse_time_ms(c.length_s);
            fade_ms   = parse_time_ms(c.fade_s);
            if (fade_ms < 0) fade_ms = 0;

            if (length_ms < 0) {
                length_ms = 170000;
                if (fade_ms <= 0) fade_ms = 10000;
                fprintf(stderr, "NOTE 没有可用的 length 标签，按默认 180 秒渲染\n");
            }
            /* 用户传了秒数：当作「想听的时长」，不能比歌曲本身还长。
               和 gsf2wav 同一口径：ms = want*1000，比 length 短就截。 */
            if (want_secs > 0.0) {
                long ms = (long)(want_secs * 1000.0);
                if (ms < length_ms) length_ms = ms;
            }

            play_ms     = length_ms + fade_ms;
            total_frames = (int64_t)play_ms * (int64_t)out_rate / 1000;
            fade_frames = (int64_t)fade_ms * (int64_t)out_rate / 1000;
            fade_start  = total_frames - fade_frames;

            out = fopen(outp, "wb");
            if (!out) { fprintf(stderr, "ERR 无法写 %s\n", outp); goto done; }
            /* 先占位，最后回填长度 */
            write_wav_header(out, 0, out_rate, 2, 16);

            /* ---- 4b. 渲染主循环（照抄 plugin.cc play()） ----
               toget = min(576, remaining)
               psx_execute(state, 0x7FFFFFFF, buf, &toget, 0)：
                 传超大 cycles = 「执行到填满 toget 个样本就停」。
               ⚠ 不能用 CYCLES_PER_FRAME（PS1 的 33868800/60）：
                 PS2 一帧要 800 样本 × 768 周期 = 614400 周期，按 PS1 的
                 564480 周期跑会截成 735 样本/帧 → 时长缩水 8%（铁证：
                 356 秒的 PSF2 只出了 327 秒）。 */
            frames_left = total_frames;
            while (frames_left > 0) {
                uint32_t toget = (frames_left > MIN_BUFFER_SIZE)
                                 ? MIN_BUFFER_SIZE : (uint32_t)frames_left;
                sint32 r = psx_execute(state, 0x7FFFFFFF, buf, &toget, 0);
                uint32_t k;

                if (r <= -2) {
                    fprintf(stderr, "WARN psx_execute 返回 %d\n", (int)r);
                    break;
                }
                if (!toget) { silent_frames++; if (silent_frames > 240) break; }
                else silent_frames = 0;

                if (toget) {
                    /* 淡出斜坡：照抄 plugin.cc play() 的 fadedness 算法。
                       samples_into_fade 从 fade_start 之后开始，逐样本递减。 */
                    int64_t samples_into_fade =
                            (total_frames - frames_left) - fade_start;
                    for (k = 0; k < toget; k++, samples_into_fade++) {
                        if (samples_into_fade > 0 && fade_frames > 0) {
                            /* ⚠ 必须用 double 乘，不能用整数除法（差 1 LSB）。 */
                            double fadedness =
                                    (double)(fade_frames - samples_into_fade)
                                    / (double)fade_frames;
                            buf[k * 2 + 0] = (int16_t)(buf[k * 2 + 0] * fadedness);
                            buf[k * 2 + 1] = (int16_t)(buf[k * 2 + 1] * fadedness);
                        }
                    }
                    for (k = 0; k < toget * 2; k++) {
                        int32_t v = buf[k];
                        if (v < 0) v = -v;
                        if (v > peak) peak = v;
                    }
                    fwrite(buf, 2, toget * 2, out);
                    total_samples += toget;
                }
                frames_left -= (int64_t)toget;
            }

            /* 回填 RIFF/data 长度 */
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
                /* 跑通了但全静音 —— 几乎都是 PSF 依赖 BIOS 行为、或者
                 * BIOS 版本不匹配。绝不当作成功。 */
                fprintf(stderr,
                        "ERR 解码完成但输出全静音（%lld 帧 / %.2f 秒）。\n"
                        "    通常是这首 PSF 依赖的 BIOS 行为没被满足，\n"
                        "    或者换一份 BIOS 试试（如 scph39001.bin）。\n",
                        total_frames - frames_left,
                        (double)total_samples / out_rate);
                rc = EXIT_SILENT;
                goto done;
            }

            printf("OK %s\n", outp);
            printf("  seconds=%.3f samples=%u peak=%d rate=%d\n",
                   (double)total_samples / out_rate, total_samples,
                   (int)peak, out_rate);
            rc = EXIT_OK;
        }
    }

done:
    if (out) fclose(out);
    if (state) free(state);
    if (emulator_extra) psf2fs_delete(emulator_extra);
    if (bios) free(bios);
    for (i = 0; i < c.n_blk; i++) free(c.blk[i].data);
    free(c.blk);
    return rc;
}
