/* wsr2wav — WonderSwan (.wsr) ROM -> WAV
 *
 * 后端是 beetle-wswan-libretro（mednafen 的 WonderSwan 核：V30MZ CPU +
 * WS 4 通道声卡 + 定时器/中断）。本文件只做「最小 libretro 前端」。
 *
 * ---- 为什么不用 mednafen 本体 ----------------------------------------
 * 它是交互式程序（必须开窗口、手动关闭），没法自动化；libretro 核是纯库，
 * 适合当解码器。
 *
 * ---- WSR 格式（依据 atwiki「soundfile」WSR 页）------------------------
 * WSR 是「裸 WonderSwan ROM 镜像」，末尾 32 字节是 WSR 头：
 *
 *   0xFFFE0  4B  "WSRF"
 *   0xFFFE4  1B  Version
 *   0xFFFE5  1B  最初の曲番号（first song number）
 *   0xFFFE6 10B  予約領域（保留）
 *   0xFFFF0  5B  x86 スタートアップ（far JMP）
 *   0xFFFF5  1B  予約
 *   0xFFFF6  2B  メーカーコード
 *   0xFFFF8  1B  タイトルコード
 *   0xFFFF9  1B  バージョン
 *   0xFFFFA  1B  ROM容量（0x04 = 16Mbit）
 *   0xFFFFB  1B  RAM容量/EEPROM容量
 *   0xFFFFC  1B  ROMのwait数
 *   0xFFFFD  1B  Sub System LSI
 *   0xFFFFE  2B  チェックサム
 *
 * 启动时寄存器：CS=0xFFFF, IP=0x0000, **AX = 演奏する曲データの番号**。
 * 「通常のWonderSwanエミュレータでは設定されない」—— 这就是核默认放不出
 * 声音的根因：v30mz_reset() 把 AX 清成 0，ROM 的启动桩读到 0 号曲就什么
 * 都不放（很多 rip 的 0 号曲本身就是空曲）。
 *
 * 反汇编 SWJ-BANC3C.wsr 的启动桩可以印证（F000:FE00）：
 *   FA            cli
 *   8B D0         mov  dx, ax        ← 先把曲号存起来
 *   33 C0         xor  ax, ax
 *   8E D0         mov  ss, ax
 *   BC 00 20      mov  sp, 0x2000
 *   52            push dx            ← 曲号入栈
 *   9A DB 4E 00 F0   call far F000:4EDB   ← 驱动初始化
 *   58            pop  ax            ← 取回曲号
 *   3D 85 00      cmp  ax, 0x0085
 *   73 05         jae  skip          ← AX>=0x85 视为「不播」
 *   9A 42 50 00 F0   call far F000:5042   ← 播放曲号 AX
 *   FB            sti
 *   EB FE         jmp  $
 *
 * 所以只要在 retro_load_game 之后（核已 Reset 完、还没跑第一条指令）把 AX
 * 设成曲号即可。核是静态链接进本程序的，直接调 v30mz_set_reg。
 *
 * ---- 曲目数怎么来 ----------------------------------------------------
 * 每个 rip 的驱动布局都不一样，分组表地址不固定，硬解析不可靠。这里用
 * 「实测」：逐个曲号跑一小段，看有没有出声。`--scan` 就是这个用途。
 * ------------------------------------------------------------------ */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdarg.h>

#include "libretro.h"

/* 来自核（mednafen/wswan/v30mz.h），静态链接所以直接可见。
 * 这里不 include 那个头，避免和 libretro.h 的 boolean.h 撞名。 */
enum {
    NEC_PC = 1, NEC_AW, NEC_CW, NEC_DW, NEC_BW, NEC_SP, NEC_BP,
    NEC_IX, NEC_IY, NEC_FLAGS, NEC_DS1, NEC_PS, NEC_SS, NEC_DS0
};
extern void v30mz_set_reg(int regnum, unsigned val);
extern unsigned v30mz_get_reg(int regnum);

static FILE *g_wav;
static int64_t g_frames;      /* 已写帧数 */
static int64_t g_target;      /* 目标帧数 */
static int g_rate = 44100;
static int g_diag;
static int g_peak;            /* 本次运行的样本绝对值峰值 */
static long long g_nz;        /* 非零样本数 */

static size_t audio_batch(const int16_t *data, size_t frames)
{
    size_t n = frames, i;
    if (g_frames + (int64_t)n > g_target)
        n = (size_t)(g_target - g_frames);
    if (n)
    {
        if (g_wav)
            fwrite(data, sizeof(int16_t) * 2, n, g_wav);
        g_frames += (int64_t)n;
    }
    for (i = 0; i < frames * 2; i++)
    {
        int v = data[i];
        if (v < 0) v = -v;
        if (v > g_peak) g_peak = v;
        if (v) g_nz++;
    }
    return frames;   /* 必须报「全部消费」，否则核会以为没写完 */
}

static void audio_one(int16_t l, int16_t r)
{
    if (g_frames < g_target)
    {
        int16_t b[2] = { l, r };
        if (g_wav) fwrite(b, sizeof(b), 1, g_wav);
        g_frames++;
    }
    {
        int a = l < 0 ? -l : l, c = r < 0 ? -r : r;
        if (a > g_peak) g_peak = a;
        if (c > g_peak) g_peak = c;
        if (l || r) g_nz++;
    }
}

static void video_cb(const void *d, unsigned w, unsigned h, size_t p)
{ (void)d; (void)w; (void)h; (void)p; }

static void input_poll(void) { }

/* 音乐模式下不按任何键（按键会打断播放） */
static int16_t input_state(unsigned port, unsigned device,
                           unsigned index, unsigned id)
{
    (void)port; (void)device; (void)index; (void)id;
    return 0;
}

static void log_cb(enum retro_log_level level, const char *fmt, ...)
{
    va_list ap;
    if (!g_diag) return;
    (void)level;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

static bool env_cb(unsigned cmd, void *data)
{
    switch (cmd)
    {
    case RETRO_ENVIRONMENT_GET_LOG_INTERFACE:
        if (data) ((struct retro_log_callback *)data)->log = log_cb;
        return true;
    case RETRO_ENVIRONMENT_GET_VARIABLE:
        /* 返回 false = 用核心默认选项 */
        if (data) ((struct retro_variable *)data)->value = NULL;
        return false;
    case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE:
        if (data) *(bool *)data = false;
        return true;
    default:
        return false;
    }
}

static void put32(FILE *f, uint32_t v) { fwrite(&v, 4, 1, f); }
static void put16(FILE *f, uint16_t v) { fwrite(&v, 2, 1, f); }

static uint8_t *g_rom;
static long g_rom_sz;

/* 载入 ROM 并把 AX 设成 track，然后跑 secs 秒。
 * wav 为 NULL 时只统计不写文件（scan 用）。返回 0 成功。 */
static int run_track(const char *path, int track, double secs, FILE *wav,
                     int *peak, long long *nz)
{
    struct retro_game_info gi;
    struct retro_system_av_info av;

    g_wav    = wav;
    g_frames = 0;
    g_peak   = 0;
    g_nz     = 0;

    retro_init();

    memset(&gi, 0, sizeof(gi));
    gi.path = path;
    gi.data = g_rom;
    gi.size = (size_t)g_rom_sz;
    gi.meta = NULL;

    if (!retro_load_game(&gi))
    {
        retro_deinit();
        return 3;
    }

    /* 核已 Reset 完，CS:IP 停在 FFFF:0000，第一条指令是 `cli; mov dx,ax`，
     * 此刻改 AX 就能被 ROM 吃到 */
    if (track >= 0)
        v30mz_set_reg(NEC_AW, (unsigned)(track & 0xFFFF));

    retro_get_system_av_info(&av);
    g_rate = (int)(av.timing.sample_rate + 0.5);
    if (g_rate <= 0) g_rate = 44100;
    g_target = (int64_t)(secs * (double)g_rate + 0.5);

    while (g_frames < g_target)
        retro_run();

    retro_unload_game();
    retro_deinit();

    if (peak) *peak = g_peak;
    if (nz)   *nz   = g_nz;
    return 0;
}

int main(int argc, char **argv)
{
    const char *in = NULL, *out = NULL;
    double secs = 150.0;
    int track = -1, i;
    int scan_lo = -1, scan_hi = -1;

    for (i = 1; i < argc; i++)
    {
        if (!strcmp(argv[i], "--secs") && i + 1 < argc)
            secs = atof(argv[++i]);
        else if (!strcmp(argv[i], "--track") && i + 1 < argc)
            track = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--scan") && i + 2 < argc)
        {
            scan_lo = atoi(argv[++i]);
            scan_hi = atoi(argv[++i]);
        }
        else if (!strcmp(argv[i], "--diag"))
            g_diag = 1;
        else if (!in)  in = argv[i];
        else if (!out) out = argv[i];
    }
    if (!in || (!out && scan_lo < 0))
    {
        fprintf(stderr, "用法:\n"
                "  wsr2wav <in.wsr> <out.wav> [--track N] [--secs S]\n"
                "  wsr2wav <in.wsr> --scan <first> <last> [--secs S]\n"
                "  --diag  输出核日志\n");
        return 2;
    }
    if (secs <= 0.0) secs = 150.0;

    /* ---- 读 ROM ---- */
    {
        FILE *fp = fopen(in, "rb");
        if (!fp) { fprintf(stderr, "打不开 %s\n", in); return 2; }
        fseek(fp, 0, SEEK_END);
        g_rom_sz = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        if (g_rom_sz <= 0) { fclose(fp); fprintf(stderr, "空文件\n"); return 2; }
        g_rom = (uint8_t *)malloc((size_t)g_rom_sz);
        if (!g_rom) { fclose(fp); fprintf(stderr, "内存不足\n"); return 2; }
        if (fread(g_rom, 1, (size_t)g_rom_sz, fp) != (size_t)g_rom_sz)
        { fclose(fp); free(g_rom); fprintf(stderr, "读失败\n"); return 2; }
        fclose(fp);
    }

    retro_set_environment(env_cb);
    retro_set_video_refresh(video_cb);
    retro_set_audio_sample(audio_one);
    retro_set_audio_sample_batch(audio_batch);
    retro_set_input_poll(input_poll);
    retro_set_input_state(input_state);

    /* ---- 扫描模式：逐个曲号跑一小段，报峰值 ---- */
    if (scan_lo >= 0)
    {
        int t;
        for (t = scan_lo; t <= scan_hi; t++)
        {
            int peak = 0;
            long long nz = 0;
            if (run_track(in, t, secs, NULL, &peak, &nz) != 0)
            {
                printf("TRACK\t%d\tERR\t0\n", t);
                continue;
            }
            printf("TRACK\t%d\t%d\t%lld\n", t, peak, nz);
            fflush(stdout);
        }
        free(g_rom);
        return 0;
    }

    /* ---- 单曲模式 ---- */
    {
        FILE *wav = fopen(out, "wb");
        if (!wav) { free(g_rom); fprintf(stderr, "写不了 %s\n", out); return 2; }
        {
            uint8_t zero[44] = {0};
            fwrite(zero, 1, 44, wav);
        }
        if (run_track(in, track, secs, wav, NULL, NULL) != 0)
        {
            fprintf(stderr, "ERR retro_load_game 失败（不是可用的 WS ROM？）\n");
            fclose(wav); free(g_rom);
            return 3;
        }
        /* 回填 WAV 头 */
        {
            long data_bytes = (long)(g_frames * 4);
            fseek(wav, 0, SEEK_SET);
            fwrite("RIFF", 1, 4, wav);
            put32(wav, (uint32_t)(36 + data_bytes));
            fwrite("WAVEfmt ", 1, 8, wav);
            put32(wav, 16);                     /* fmt chunk size */
            put16(wav, 1);                      /* PCM */
            put16(wav, 2);                      /* channels */
            put32(wav, (uint32_t)g_rate);
            put32(wav, (uint32_t)(g_rate * 4)); /* byte rate */
            put16(wav, 4);                      /* block align */
            put16(wav, 16);                     /* bits per sample */
            fwrite("data", 1, 4, wav);
            put32(wav, (uint32_t)data_bytes);
        }
        fclose(wav);

        printf("OK %s  %.2fs @%dHz  %lld frames  peak=%d\n",
               out, (double)g_frames / (double)g_rate, g_rate,
               (long long)g_frames, g_peak);
    }

    free(g_rom);
    return 0;
}
