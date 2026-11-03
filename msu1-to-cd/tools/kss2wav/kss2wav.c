/* kss2wav.c — KSS / MSX + SMS/GG 芯片音源 -> WAV（libkss 后端）
 *
 * 为什么需要它
 * ------------
 * 本工程的 libgme.dll 对 KSS 的支持是**残缺的**：它只映射 0x0000-0x3FFF 与
 * 0x8000-0xBFFF，不映射 0x4000-0x7FFF。于是「ROM 落在 0x4000-0x7FFF」的 KSS
 * （例如 Zophar / joshw 的 Game Gear《Puyo Puyo》G-3324.kss，32 KB、load 到
 * 0x3E10）会丢掉 16 KB 代码，Z80 直接跑飞 —— 表现是**每一轨都精确静音**
 * （peak == 0），libgme 却不报任何错。
 *
 * 实测（12 个 Zophar GG 包抽样）：SGC 全通；KSS 里 16 KB 布局的通，
 * 32 KB / 8 KB 布局的静音，约占三成。这不是素材坏了 —— 同一份
 * G-3324.kss 在 joshw 的 Game Gear 目录里一模一样，libgme 同样解不了。
 *
 * libkss（digital-sound-antiques）是完整的 KSS 实现：支持 MSX（AY-3-8910 /
 * OPLL / SCC）与 SMS/GG（SN76489），内存映射与 bank 切换都按规范做。
 * 同一个文件它能正常出声（实测 peak 4017）。
 *
 * 用法
 * ----
 *     kss2wav.exe -s<song> -p<secs> -r<rate> -n<1|2> -o<out.wav> <input.kss>
 *
 * 与上游 example/kss2wav.c 的差别（都是为了配合本工程的流水线）：
 *   1. **不做 fade**。整条流水线的淡入淡出统一由 Python 侧做（fade_ms），
 *      这里再淡一次就是双重衰减。上游 `-f0` 会立刻 FADE_END 只出 1 秒，
 *      所以干脆把 fade 逻辑删掉。
 *   2. **不做 loop 检测**。时长由调用方给定（来自 m3u 的真实时长）。
 *   3. 去掉 stderr 的 `\b` 进度刷屏（打包成窗口程序后是噪音），
 *      改成一行 `OK song=%d frames=%d peak=%d`，便于 Python 侧判空。
 *   4. 输出固定 16 bit 小端。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "src/libkss/src/kssplay.h"

#define MAX_PATH_LEN 1024

static void put16(unsigned char *p, unsigned int v) {
  p[0] = (unsigned char)(v & 0xff);
  p[1] = (unsigned char)((v >> 8) & 0xff);
}

static void put32(unsigned char *p, unsigned int v) {
  p[0] = (unsigned char)(v & 0xff);
  p[1] = (unsigned char)((v >> 8) & 0xff);
  p[2] = (unsigned char)((v >> 16) & 0xff);
  p[3] = (unsigned char)((v >> 24) & 0xff);
}

static void usage(void) {
  fprintf(stderr,
          "usage: kss2wav -s<song> -p<secs> [-r<rate>] [-n<1|2>] -o<out.wav> IN.kss\n"
          "  -s  song index (0-based, default 0)\n"
          "  -p  play seconds (default 60)\n"
          "  -r  sample rate (default 44100)\n"
          "  -n  channels 1 or 2 (default 1)\n"
          "  -o  output wav (required)\n");
}

int main(int argc, char **argv) {
  int song = 0;
  int secs = 60;
  int rate = 44100;
  int nch = 1;
  char input[MAX_PATH_LEN];
  char output[MAX_PATH_LEN];
  int i;

  KSS *kss;
  KSSPLAY *player;
  FILE *fp;
  short *buf;
  unsigned char hdr[44];
  long written = 0;
  int peak = 0;
  int t;

  input[0] = '\0';
  output[0] = '\0';

  for (i = 1; i < argc; i++) {
    const char *a = argv[i];
    if (a[0] == '-' && a[1] != '\0') {
      switch (a[1]) {
      case 's':
        song = atoi(a + 2);
        break;
      case 'p':
        secs = atoi(a + 2);
        break;
      case 'r':
        rate = atoi(a + 2);
        break;
      case 'n':
        nch = (atoi(a + 2) == 2) ? 2 : 1;
        break;
      case 'o':
        strncpy(output, a + 2, MAX_PATH_LEN - 1);
        output[MAX_PATH_LEN - 1] = '\0';
        break;
      default:
        usage();
        return 2;
      }
    } else {
      strncpy(input, a, MAX_PATH_LEN - 1);
      input[MAX_PATH_LEN - 1] = '\0';
    }
  }

  if (input[0] == '\0' || output[0] == '\0') {
    usage();
    return 2;
  }
  if (secs <= 0)
    secs = 1;
  if (rate < 8000 || rate > 192000)
    rate = 44100;

  if ((kss = KSS_load_file(input)) == NULL) {
    fprintf(stderr, "ERR cannot load %s\n", input);
    return 3;
  }

  /* 曲号越界：libkss 会静音，这里提前挡掉，报错比给空 WAV 好 */
  if (song < 0) {
    fprintf(stderr, "ERR song index %d < 0\n", song);
    KSS_delete(kss);
    return 4;
  }

  if ((fp = fopen(output, "wb")) == NULL) {
    fprintf(stderr, "ERR cannot write %s\n", output);
    KSS_delete(kss);
    return 5;
  }

  if ((player = KSSPLAY_new((uint32_t)rate, (uint32_t)nch, 16)) == NULL) {
    fprintf(stderr, "ERR KSSPLAY_new failed\n");
    fclose(fp);
    KSS_delete(kss);
    return 6;
  }

  KSSPLAY_set_data(player, kss);
  KSSPLAY_reset(player, song, 0);
  KSSPLAY_set_device_quality(player, KSS_DEVICE_PSG, 1);
  KSSPLAY_set_device_quality(player, KSS_DEVICE_SCC, 1);
  KSSPLAY_set_device_quality(player, KSS_DEVICE_OPLL, 1);

  /* 先写占位头，跑完回填 */
  memset(hdr, 0, sizeof(hdr));
  memcpy(hdr, "RIFF", 4);
  memcpy(hdr + 8, "WAVEfmt ", 8);
  put32(hdr + 16, 16);
  put16(hdr + 20, 1);                 /* PCM */
  put16(hdr + 22, (unsigned int)nch);
  put32(hdr + 24, (unsigned int)rate);
  put32(hdr + 28, (unsigned int)(rate * nch * 2));
  put16(hdr + 32, (unsigned int)(nch * 2));
  put16(hdr + 34, 16);
  memcpy(hdr + 36, "data", 4);
  fwrite(hdr, 1, sizeof(hdr), fp);

  if ((buf = (short *)malloc(sizeof(short) * (size_t)rate * (size_t)nch)) == NULL) {
    fprintf(stderr, "ERR out of memory\n");
    fclose(fp);
    KSSPLAY_delete(player);
    KSS_delete(kss);
    return 7;
  }

  for (t = 0; t < secs; t++) {
    int n = rate * nch;
    int k;
    KSSPLAY_calc(player, buf, (uint32_t)rate);
    for (k = 0; k < n; k++) {
      int v = buf[k];
      int av = v < 0 ? -v : v;
      if (av > peak)
        peak = av;
    }
    /* 统一按小端写（x86 上就是内存序，显式转换是为了可移植） */
    for (k = 0; k < n; k++) {
      unsigned char b[2];
      put16(b, (unsigned int)(unsigned short)buf[k]);
      fwrite(b, 1, 2, fp);
      written += 2;
    }
    if (KSSPLAY_get_stop_flag(player)) {
      break;
    }
  }

  free(buf);
  KSSPLAY_delete(player);
  KSS_delete(kss);

  fseek(fp, 0, SEEK_SET);
  put32(hdr + 4, (unsigned int)(36 + written));
  put32(hdr + 40, (unsigned int)written);
  fwrite(hdr, 1, sizeof(hdr), fp);
  fclose(fp);

  fprintf(stderr, "OK song=%d frames=%ld peak=%d\n", song, written / (2 * nch), peak);
  return 0;
}
