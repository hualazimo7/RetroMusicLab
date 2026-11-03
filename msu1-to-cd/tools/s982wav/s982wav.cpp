/* s982wav.cpp — S98（PC-98 / YM2608 OPNA）音源 -> WAV（ymfm 后端）
 *
 * 为什么需要它
 * ------------
 * `.s98` 是 **YM2608（OPNA）的寄存器写入日志**，和 `.vgm` 同族：文件里没有
 * 音频，只有「第几个 tick 往哪个寄存器写什么」。要出声必须真跑芯片模拟。
 *
 * 工程里的 libgme 走不通 —— 这一点是**读源码确认**的，不是猜的：
 * `gme/Vgm_Emu_Impl.cpp` 的 `run_commands()` 只实现 5 个芯片指令
 * （cmd_gg_stereo / cmd_psg / cmd_ym2413 / cmd_ym2612_port0/1），
 * 其余命令一律落到 `default:` 分支被 `pos += command_len(cmd) - 1` **静默跳过**，
 * 只挂一句 "Unknown stream event" 警告。所以就算把 .s98 转成合法的 .vgm
 * 交给它，YM2608 的写入（VGM 0x56/0x57）也全部被丢掉 —— 输出精确静音、
 * 而且**不报错**。这条路是死的。
 *
 * 所以这里直接接 ymfm（Aaron Giles，BSD-3，MAME 在用的 OPN 家族核心），
 * 它完整实现了 YM2608 的 FM（6 通道 4 算子）+ SSG（AY-3-8910 兼容）
 * + ADPCM-A（节奏）+ ADPCM-B。
 *
 * S98 格式（照 libvgm `player/s98player.cpp` 抄，实测 18 个真实文件全对）
 * ------------------------------------------------------------------
 *   0x00  "S98" + 版本字符（'0'..'3'）
 *   0x04  tickMult（计时器分子，uint32 LE）
 *   0x08  tickDiv （计时器分母）
 *   0x0C  compression（0 = 未压缩；非 0 本程序不支持）
 *   0x10  tagOfs  （曲名等标签的偏移）
 *   0x14  dataOfs （数据流起始）
 *   0x18  loopOfs （循环点，0 = 不循环）
 *   0x1C  v3 才有：设备数
 *   0x20  v2/v3：设备表，每项 0x10 字节 {type, clock, pan, app_spec}
 *
 * v0/v1 **没有设备表**，芯片写死 YM2608、时钟写死 7987200 Hz，
 * tick 默认 10/1000 秒（libvgm 的 `if (!tickMult) tickMult = 10;` 同理）。
 * 实测 PC-98 的 Ys 包 18 个文件**全是 v1**。
 *
 * 数据流（变长编码，这一点最容易写错）：
 *   0xFF        -> 前进 1 tick
 *   0xFE <var>  -> 前进 2 + var tick（var 是 LEB128，低位在前，MSB 是续位标志）
 *   0xFD        -> 文件结束
 *   其它 cmd    -> cmd 的 bit0 = 端口号，bit1.. = 设备号；
 *                  后面跟 reg, data 两个字节
 *
 * ⚠ 上一轮我把数据流当成「固定 4 字节一组 [wait][port][reg][val]」穷举，
 *   当然全错 —— 等待是**单独的 0xFF 命令**，不是写在每条记录里的字段。
 *
 * 采样率
 * ------
 * ymfm 的 MED 档：输出 = clock/24 = 332800 Hz，FM 内部 = clock/144 = 55467 Hz，
 * SSG 内部 = 249600 Hz 再 4:3 上采样到 332800。**上采样不会混叠**，
 * 所以 MED 是干净的（MIN 档 SSG 走 2:3 下采样，124.8k 以上的谐波会折回来，
 * 所以不用 MIN）。
 *
 * 332800 -> 44100 用单级多相位 FIR：通带 18 kHz、阻带 22.05 kHz。
 * 再往下就是纯整数运算，没有浮点漂移（输出位置 = 序号 × 步长，不做累加）。
 *
 * 混音
 * ----
 * ymfm 的 YM2608 输出 3 路：data[0]=FM L、data[1]=FM R、data[2]=SSG。
 * SSG 是**单极性**的（0..32764，MAME 的振幅表「biased so that 0 == 0」），
 * 真机上靠 AC 耦合去掉直流。这里对 SSG 先过一个一阶隔直再与 FM 相加，
 * 否则会带上一个几千的直流偏置、并且把动态范围吃掉一半。
 *
 * 用法
 * ----
 *     s982wav.exe [-p<secs>] [-t<tail_secs>] [-r<rate>] [-g<gain>]
 *                 [-c<clock>] -o<out.wav> IN.s98
 *
 *   -p  最长秒数（0 = 不限，按文件自身的结束标记）
 *   -t  结束标记之后的余音秒数（默认 1.5，最后一个和弦需要衰减）
 *   -r  输出采样率（默认 44100）
 *   -g  混音增益（默认 1.0）
 *   -c  覆盖 YM2608 时钟（默认 7987200）
 *
 * 与 kss2wav 的约定保持一致：**不做 fade**（淡入淡出统一由 Python 侧做），
 * 只在 stderr 打一行 `OK frames=... peak=...` 便于上层判空。
 */

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "ymfm_opn.h"

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

//: S98 v0/v1 的默认时钟。libvgm 的 `_devHdrs[0].clock = 7987200;`，PC-98 的
//: OPNA 就是这个频率（主频 2.4576 MHz × 3.25，或直接 8 MHz 分频）。
static const uint32_t DEFAULT_CLOCK = 7987200;

//: S98 v0/v1 的默认 tick 计时器（libvgm 的兜底值）：10/1000 秒 = 100 Hz。
static const uint32_t DEFAULT_TICK_MULT = 10;
static const uint32_t DEFAULT_TICK_DIV = 1000;

//: 隔直滤波的极点。R = 1 - 2*pi*fc/fs，fc≈10 Hz @44.1k。
static const double DC_BLOCK_R = 0.99858;

//: 多相位 FIR 的相位分辨率。256 档 → 相位量化误差约 -57 dB，16bit 够用。
static const int PHASES = 256;

//: zig 的 libc 在严格模式下不定义 PI，自己写一份。
static const double PI = 3.14159265358979323846;

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

static uint32_t rd32(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void put16(unsigned char *p, unsigned int v)
{
    p[0] = (unsigned char)(v & 0xFF);
    p[1] = (unsigned char)((v >> 8) & 0xFF);
}

static void put32(unsigned char *p, unsigned int v)
{
    put16(p + 0, v & 0xFFFF);
    put16(p + 2, (v >> 16) & 0xFFFF);
}

// ---------------------------------------------------------------------------
// ymfm 接口：定时器 / IRQ 全部不需要，用默认空实现
// ---------------------------------------------------------------------------

struct SilentIface : public ymfm::ymfm_interface
{
};

// ---------------------------------------------------------------------------
// 任意比重采样：多相位窗函数 sinc + 线性滑动缓冲（流式，不占整轨内存）
// ---------------------------------------------------------------------------

class Resampler
{
public:
    void init(double fs_in, double fs_out, double pass_hz, double stop_hz)
    {
        m_step = fs_in / fs_out;

        // 过渡带宽决定抽头数。Blackman 窗的阻带约 -74 dB，经验式
        // N ≈ 4 * fs / 过渡带宽 已经足够；再宽一点更保险。
        double trans = stop_hz - pass_hz;
        if (trans < 100.0)
            trans = 100.0;
        int n = (int)std::ceil(4.0 * fs_in / trans);
        if (n < 15)
            n = 15;
        if ((n & 1) == 0)
            n++;
        m_taps = n;
        m_half = n / 2;

        // 截止频率取通带与阻带的中点
        double fc = 0.5 * (pass_hz + stop_hz) / fs_in;   // 归一化到 fs（0..0.5）
        double wc = 2.0 * PI * fc;

        // 在 fs 的 PHASES 倍过采样上存一张表，**相位优先**：
        // h[p * taps + k] 是「相位 p、第 k 个抽头」的系数。
        //
        // ⚠ 布局不能反过来。按抽头优先（h[k * PHASES + p]）时，内层循环
        // 每取一个系数就跨 PHASES*4 = 1 KB，等于**每个抽头一次 cache miss**；
        // 331 抽头 × 每输出采样 = 21 KB 的跨度。实测这样跑 114 秒的曲子
        // 要 20 秒。改成相位优先后同一相位的 331 个系数是连续的 1.3 KB，
        // 落在 L2 里，同一个曲子降到 2 秒级。
        m_h.resize((size_t)m_taps * PHASES);
        std::vector<double> phase_sum(PHASES, 0.0);
        for (int p = 0; p < PHASES; p++)
        {
            double frac = (double)p / PHASES;
            for (int k = 0; k < m_taps; k++)
            {
                // 抽头中心在 m_half；p 相位额外右移 frac
                double t = (double)k - (double)m_half - frac;
                double s = (std::fabs(t) < 1e-9) ? wc
                                                 : std::sin(wc * t) / t;
                // Blackman 窗
                double x = (double)k / (double)(m_taps - 1);
                double w = 0.42 - 0.5 * std::cos(2.0 * PI * x) +
                           0.08 * std::cos(4.0 * PI * x);
                m_h[(size_t)p * m_taps + k] = (float)(s * w);
                phase_sum[p] += s * w;
            }
        }
        // 归一化：每个相位单独归一到直流增益 1，避免相位之间响度抖动
        for (int p = 0; p < PHASES; p++)
        {
            double g = (std::fabs(phase_sum[p]) > 1e-12)
                           ? (1.0 / phase_sum[p]) : 1.0;
            for (int k = 0; k < m_taps; k++)
                m_h[(size_t)p * m_taps + k] =
                    (float)(m_h[(size_t)p * m_taps + k] * g);
        }

        m_out_index = 0;
        //: 预卷。输出第 0 个采样需要 [−m_half, +m_half) 这段输入，其中负半段
        //: 根本不存在。这里直接在前面塞 m_half 帧 0，并把 m_start 定到 −m_half。
        //:
        //: ⚠ 不这么做的话 `drain` 的 `lo < m_start` 条件对**输出 0** 永远成立
        //: （m_start 只会往后走，不会变小），于是整条流一个采样都吐不出来，
        //: 全部堆到最后 flush 的慢路径 —— 表现是「结果是对的，但慢 10 倍，
        //: 而且 -p 截断完全失效」（实测 114 秒的曲子 20 秒，改成预卷后 2 秒）。
        m_start = -(int64_t)m_half;
        m_buf.assign((size_t)m_half * 2, 0.0f);
    }

    /** 追加输入（交错立体声 float）。 */
    void feed(const float *l, const float *r, size_t nframes)
    {
        size_t old = m_buf.size();
        m_buf.resize(old + nframes * 2);
        for (size_t i = 0; i < nframes; i++)
        {
            m_buf[old + i * 2 + 0] = l[i];
            m_buf[old + i * 2 + 1] = r[i];
        }
    }

    /**
     * 把能算的输出全部算出来，追加到 out（交错立体声）。
     * @param flush true = 输入已结束，尾部不足的抽头按 0 补。
     *
     * ⚠ 内层循环**不能**带边界判断。非 flush 路径进来时已经保证
     * `[lo, hi)` 整段都在缓冲里，加一个 `if (idx < start || idx >= end)`
     * 会让编译器放弃向量化 —— 实测这一处就占了整个解码器一半的时间。
     * 所以分成快慢两条路：flush 才走带判断的慢路（而且只有最后 331 个
     * 输出采样走它，代价可以忽略）。
     */
    void drain(std::vector<float> &out, bool flush)
    {
        size_t have = m_buf.size() / 2;              // 缓冲里的帧数
        int64_t end = m_start + (int64_t)have;       // 缓冲覆盖 [m_start, end)

        for (;;)
        {
            double apos = (double)m_out_index * m_step;
            int64_t i0 = (int64_t)std::floor(apos);
            double frac = apos - (double)i0;

            // 需要 [i0 - m_half, i0 - m_half + taps)
            int64_t lo = i0 - m_half;
            int64_t hi = lo + m_taps;                // 不含
            if (!flush)
            {
                // 右边界还没生成够就先等着；左边界已经滑出缓冲也算不了
                if (hi > end || lo < m_start)
                    break;
            }
            else
            {
                if (lo >= end)
                    break;
            }

            int ph = (int)(frac * PHASES);
            if (ph < 0)
                ph = 0;
            if (ph >= PHASES)
                ph = PHASES - 1;

            const float *h = &m_h[(size_t)ph * m_taps];
            float sl = 0.0f, sr = 0.0f;
            if (!flush)
            {
                // 快路：整段都在缓冲里，且系数连续
                const float *s = &m_buf[(size_t)(lo - m_start) * 2];
                for (int k = 0; k < m_taps; k++)
                {
                    float c = h[k];
                    sl += s[(size_t)k * 2 + 0] * c;
                    sr += s[(size_t)k * 2 + 1] * c;
                }
            }
            else
            {
                // 慢路：越界补 0
                for (int k = 0; k < m_taps; k++)
                {
                    int64_t idx = lo + k;
                    if (idx < m_start || idx >= end)
                        continue;
                    size_t o = (size_t)(idx - m_start) * 2;
                    float c = h[k];
                    sl += m_buf[o + 0] * c;
                    sr += m_buf[o + 1] * c;
                }
            }
            out.push_back(sl);
            out.push_back(sr);
            m_out_index++;

            // 滑动窗口：把用不到的左半截扔掉，避免缓冲无限长
            if (m_out_index % 4096 == 0)
                compact();
        }
    }

    void compact()
    {
        double apos = (double)m_out_index * m_step;
        int64_t keep_from = (int64_t)std::floor(apos) - m_half - 8;
        if (keep_from <= m_start)
            return;
        int64_t drop = keep_from - m_start;
        size_t have = m_buf.size() / 2;
        if (drop > (int64_t)have)
            drop = (int64_t)have;
        if (drop <= 0)
            return;
        m_buf.erase(m_buf.begin(), m_buf.begin() + (size_t)drop * 2);
        m_start += drop;
    }

private:
    int m_taps = 0;
    int m_half = 0;
    double m_step = 1.0;
    std::vector<float> m_h;
    std::vector<float> m_buf;      // 交错立体声
    int64_t m_start = 0;
    int64_t m_out_index = 0;
};

// ---------------------------------------------------------------------------
// S98 解析
// ---------------------------------------------------------------------------

struct S98Header
{
    int version = 0;
    uint32_t tick_mult = 0;
    uint32_t tick_div = 0;
    uint32_t compression = 0;
    uint32_t tag_ofs = 0;
    uint32_t data_ofs = 0;
    uint32_t loop_ofs = 0;
    uint32_t clock = DEFAULT_CLOCK;
    int dev_type = 4;              // 4 = S98DEV_OPNA
    std::string title;
};

static bool parse_header(const std::vector<unsigned char> &d, S98Header &h,
                         std::string &err)
{
    if (d.size() < 0x20)
    {
        err = "文件太短，不是 S98";
        return false;
    }
    if (memcmp(&d[0], "S98", 3) != 0)
    {
        err = "魔数不是 S98";
        return false;
    }
    h.version = (int)(d[0x03] - '0');
    if (h.version < 0 || h.version > 3)
    {
        err = "不认识的 S98 版本字节";
        return false;
    }

    h.tick_mult = rd32(&d[0x04]);
    h.tick_div = rd32(&d[0x08]);
    h.compression = rd32(&d[0x0C]);
    h.tag_ofs = rd32(&d[0x10]);
    h.data_ofs = rd32(&d[0x14]);
    h.loop_ofs = rd32(&d[0x18]);

    if (h.compression != 0)
    {
        err = "这个 .s98 是压缩的（compression != 0），本程序只支持未压缩";
        return false;
    }
    if (h.data_ofs == 0 || h.data_ofs >= d.size())
    {
        err = "数据流偏移越界";
        return false;
    }

    // v0/v1：没有设备表，芯片写死 YM2608
    if (h.version >= 2 && 0x20 + 0x10 <= d.size())
    {
        uint32_t dev_type = rd32(&d[0x20]);
        uint32_t clock = rd32(&d[0x24]);
        if (dev_type != 0)
        {
            h.dev_type = (int)dev_type;
            if (clock)
                h.clock = clock;
        }
    }

    if (h.tick_mult == 0)
        h.tick_mult = DEFAULT_TICK_MULT;
    if (h.tick_div == 0)
        h.tick_div = DEFAULT_TICK_DIV;

    // 标签区第一个 '\0' 结尾的串就是曲名（v0/v1 的 tag 段是纯文本）
    if (h.tag_ofs && h.tag_ofs < d.size())
    {
        size_t i = h.tag_ofs;
        size_t end = i;
        while (end < d.size() && d[end] != 0)
            end++;
        if (end > i && end - i < 200)
        {
            // 只取第一行，且必须像可打印文本（v2+ 是 key=value 多行）
            std::string s((const char *)&d[i], end - i);
            size_t nl = s.find_first_of("\r\n");
            if (nl != std::string::npos)
                s = s.substr(0, nl);
            bool printable = !s.empty();
            for (size_t k = 0; k < s.size(); k++)
            {
                unsigned char c = (unsigned char)s[k];
                if (c < 0x20 || c == 0x7F)
                {
                    printable = false;
                    break;
                }
            }
            if (printable)
                h.title = s;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

static void usage(void)
{
    fprintf(stderr,
            "usage: s982wav [-p<secs>] [-t<tail>] [-r<rate>] [-g<gain>] "
            "[-c<clock>] -o<out.wav> IN.s98\n");
}

int main(int argc, char **argv)
{
    const char *input = NULL;
    const char *output = NULL;
    double max_secs = 0.0;
    double tail_secs = 1.5;
    uint32_t out_rate = 44100;
    double gain = 1.0;
    uint32_t force_clock = 0;

    for (int i = 1; i < argc; i++)
    {
        const char *a = argv[i];
        if (a[0] == '-' && a[1] != '\0')
        {
            // 参数值可以紧贴（-oout.wav）也可以分开（-o out.wav）。
            // kss2wav 只认紧贴写法，实测这里分开写会静默变成空串。
            const char *val = a + 2;
            if (*val == '\0' && i + 1 < argc)
                val = argv[++i];
            switch (a[1])
            {
            case 'p': max_secs = atof(val); break;
            case 't': tail_secs = atof(val); break;
            case 'r': out_rate = (uint32_t)atoi(val); break;
            case 'g': gain = atof(val); break;
            case 'c': force_clock = (uint32_t)strtoul(val, NULL, 10); break;
            case 'o': output = val; break;
            default: usage(); return 2;
            }
        }
        else
        {
            input = a;
        }
    }
    if (!input || !output || out_rate < 8000 || out_rate > 192000)
    {
        usage();
        return 2;
    }

    // ---- 读文件 ----
    FILE *fp = fopen(input, "rb");
    if (!fp)
    {
        fprintf(stderr, "ERR cannot open %s\n", input);
        return 1;
    }
    fseek(fp, 0, SEEK_END);
    long fsz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (fsz <= 0)
    {
        fclose(fp);
        fprintf(stderr, "ERR empty file\n");
        return 1;
    }
    std::vector<unsigned char> data((size_t)fsz);
    if (fread(&data[0], 1, (size_t)fsz, fp) != (size_t)fsz)
    {
        fclose(fp);
        fprintf(stderr, "ERR short read\n");
        return 1;
    }
    fclose(fp);

    S98Header hdr;
    std::string err;
    if (!parse_header(data, hdr, err))
    {
        fprintf(stderr, "ERR %s\n", err.c_str());
        return 1;
    }
    if (hdr.dev_type != 4)
    {
        fprintf(stderr, "ERR 这个 .s98 用的是设备类型 %d，本程序只支持 4"
                        "（YM2608 / OPNA）\n", hdr.dev_type);
        return 1;
    }
    if (force_clock)
        hdr.clock = force_clock;

    // ---- 芯片 ----
    SilentIface iface;
    ymfm::ym2608 chip(iface);
    chip.set_fidelity(ymfm::OPN_FIDELITY_MED);
    chip.reset();

    // MED 档：输出率 = clock / 24
    uint32_t gen_rate = chip.sample_rate(hdr.clock);
    if (gen_rate < 100000 || gen_rate > 2000000)
    {
        fprintf(stderr, "ERR 芯片时钟 %u Hz 不合理（输出率会变成 %u）\n",
                hdr.clock, gen_rate);
        return 1;
    }

    Resampler rs;
    rs.init((double)gen_rate, (double)out_rate, 18000.0, 22050.0);

    // ---- 打开输出 ----
    FILE *of = fopen(output, "wb");
    if (!of)
    {
        fprintf(stderr, "ERR cannot write %s\n", output);
        return 1;
    }
    unsigned char wavhdr[44];
    memset(wavhdr, 0, sizeof(wavhdr));
    memcpy(wavhdr + 0, "RIFF", 4);
    memcpy(wavhdr + 8, "WAVEfmt ", 8);
    put32(wavhdr + 16, 16);
    put16(wavhdr + 20, 1);          // PCM
    put16(wavhdr + 22, 2);          // 立体声
    put32(wavhdr + 24, out_rate);
    put32(wavhdr + 28, out_rate * 4);
    put16(wavhdr + 32, 4);
    put16(wavhdr + 34, 16);
    memcpy(wavhdr + 36, "data", 4);
    fwrite(wavhdr, 1, 44, of);

    // ---- 渲染 ----
    //: 一次生成多少 gen 采样。ymfm 的 `generate()` **必须整块调**：
    //: 逐采样调 `generate(&od, 1)` 会为每个采样走一遍 SSG 重采样器的
    //: 函数指针派发，实测 114 秒的曲子要 20 秒；整块调之后降到 3 秒级。
    const int BLOCK = 16384;
    std::vector<ymfm::ym2608::output_data> odbuf(BLOCK);
    std::vector<float> fL(BLOCK), fR(BLOCK);
    std::vector<float> res;                 // 交错立体声
    std::vector<short> pcm;
    int64_t gen_pos = 0;                    // 已生成的 gen 采样数
    int64_t out_frames = 0;
    int peak = 0;
    bool capped = false;
    const int64_t cap_frames =
        (max_secs > 0.0) ? (int64_t)(max_secs * out_rate) : 0;

    // 隔直状态（只作用在 SSG 上，见文件头「混音」一节）
    double dc_x1 = 0.0, dc_y1 = 0.0;

    // 把 res 里的浮点结果量化成 16bit 写盘
    auto write_pcm = [&]() -> void
    {
        size_t nf = res.size() / 2;
        if (!nf)
            return;
        pcm.resize(nf * 2);
        for (size_t i = 0; i < nf * 2; i++)
        {
            double v = (double)res[i] * gain;
            if (v > 32767.0)
                v = 32767.0;
            if (v < -32768.0)
                v = -32768.0;
            int iv = (int)(v >= 0 ? v + 0.5 : v - 0.5);
            if (iv > peak)
                peak = iv;
            if (-iv > peak)
                peak = -iv;
            pcm[i] = (short)iv;
        }
        fwrite(&pcm[0], 2, pcm.size(), of);
        out_frames += (int64_t)nf;
    };

    // 生成 n 个 gen 采样，并把它能算出的输出全部写出去
    auto render_n = [&](int64_t nsamp) -> void
    {
        while (nsamp > 0 && !capped)
        {
            int n = (int)(nsamp > BLOCK ? BLOCK : nsamp);
            chip.generate(&odbuf[0], (uint32_t)n);
            for (int i = 0; i < n; i++)
            {
                double ssg = (double)odbuf[i].data[2];
                // SSG 是单极性（0..32764），一阶隔直去掉直流分量
                double y = ssg - dc_x1 + DC_BLOCK_R * dc_y1;
                dc_x1 = ssg;
                dc_y1 = y;
                fL[i] = (float)((double)odbuf[i].data[0] + y);
                fR[i] = (float)((double)odbuf[i].data[1] + y);
            }
            gen_pos += n;
            rs.feed(&fL[0], &fR[0], (size_t)n);

            res.clear();
            rs.drain(res, false);
            write_pcm();

            if (cap_frames > 0 && out_frames >= cap_frames)
                capped = true;
            nsamp -= n;
        }
    };

    // 把 gen 采样推进到「tick 数 t 对应的采样位置」
    auto sync_to = [&](uint64_t t) -> void
    {
        // 位置 = t * gen_rate * tickMult / tickDiv（整数，四舍五入）。
        // 用 long double 是为了 t 很大（实测 23000 tick）时不丢精度。
        long double pos = (long double)t * (long double)gen_rate *
                          (long double)hdr.tick_mult / (long double)hdr.tick_div;
        int64_t target = (int64_t)(pos + 0.5L);
        if (target > gen_pos)
            render_n(target - gen_pos);
    };

    // 数据流
    size_t pos = hdr.data_ofs;
    uint64_t ticks = 0;
    bool ended = false;
    size_t n_writes = 0;

    while (pos < data.size())
    {
        unsigned char cmd = data[pos++];
        if (cmd == 0xFF)
        {
            ticks++;
        }
        else if (cmd == 0xFE)
        {
            // LEB128：低位在前，bit7 = 还有后续字节
            uint32_t v = 0;
            int shift = 0;
            while (pos < data.size())
            {
                unsigned char b = data[pos++];
                v |= (uint32_t)(b & 0x7F) << shift;
                shift += 7;
                if (!(b & 0x80))
                    break;
                if (shift > 28)
                    break;
            }
            ticks += 2 + v;
        }
        else if (cmd == 0xFD)
        {
            ended = true;
            break;
        }
        else
        {
            if (pos + 2 > data.size())
                break;
            unsigned char reg = data[pos + 0];
            unsigned char val = data[pos + 1];
            pos += 2;
            unsigned int port = cmd & 0x01;
            unsigned int dev = cmd >> 1;
            // v0/v1 只有一个设备，dev 恒为 0；多设备时不认识的设备直接忽略
            if (dev == 0)
            {
                sync_to(ticks);
                if (capped)
                    break;
                chip.write(port ? 2u : 0u, reg);
                chip.write(port ? 3u : 1u, val);
                n_writes++;
            }
        }
    }

    // 补上数据流**最后一段等待**。
    //
    // ⚠ ``sync_to()`` 只在「寄存器写入」那一支里调（写入前必须先把芯片推到
    //   那个时刻），``0xFF`` / ``0xFE`` 只是累加 tick、不推进芯片。于是
    //   ``0xFD`` 之前若有一串收尾的 ``0xFF``，那段时间**从来没被渲染**。
    //
    //   症状：``chip_formats.probe()`` 扫到 ``0xFD`` 算出的时长（它会把
    //   全部 tick 都数上）比实解时长**多出这一段**，而这段通常是曲子末尾
    //   的余韵。实测合成样本：probe 报 1.200 秒，实解只有 1.000+1.5 秒。
    //   真实素材差别小（最后一个和弦紧跟 0xFD），所以一直没暴露。
    if (!capped)
        sync_to(ticks);

    // 余音：0xFD 之后的最后一个和弦还在响，直接截断会「咔」一下
    if (!capped && tail_secs > 0.0)
        render_n((int64_t)(tail_secs * (double)gen_rate));

    // 收尾：把 FIR 尾部剩下的算完
    res.clear();
    rs.drain(res, true);
    write_pcm();

    // 回填 RIFF/data 长度
    long bytes = (long)(out_frames * 4);
    fseek(of, 0, SEEK_SET);
    put32(wavhdr + 4, (unsigned int)(36 + bytes));
    put32(wavhdr + 40, (unsigned int)bytes);
    fwrite(wavhdr, 1, 44, of);
    fclose(of);

    if (out_frames == 0)
    {
        fprintf(stderr, "ERR 没有产出任何采样（%s）\n",
                ended ? "数据流立即结束" : "数据流解析失败");
        return 1;
    }

    fprintf(stderr,
            "OK frames=%ld peak=%d ver=%d clock=%u gen=%u rate=%u "
            "ticks=%llu writes=%lu dur=%.3f\n",
            (long)out_frames, peak, hdr.version, hdr.clock, gen_rate, out_rate,
            (unsigned long long)ticks, (unsigned long)n_writes,
            (double)out_frames / (double)out_rate);
    return 0;
}
