/**
 * @file    pid_comm.c
 * @brief   SerialPID-Tuner 串口协议层实现（无 HAL 依赖）
 *
 * 解帧策略与 PC 侧 FrameDecoder 等价：
 *   1) 逐字节累积到解析窗口
 *   2) 找 AA 55 -> 读 LEN -> 读 TYPE -> 等够 total 字节
 *   3) 校验帧尾（按本帧自身偏移，不是缓冲区末尾）
 *   4) 校验 CRC16，通过才回调
 *   5) 任一步失败则丢弃 1 字节后重新同步
 *
 * 与 PC 侧唯一有意的差异：
 *   PC 的 FrameDecoder 帧尾检查用的是整个缓冲区末尾 buf[-4:]，
 *   当一帧后面紧跟非 0x77 的垃圾字节时会误丢这一帧；
 *   MCU 侧按帧内偏移 win[total-4 .. total-1] 校验，行为更正确。
 *   （合法流上两者结果完全一致，见 tools/gen_protocol_vectors.py 的对比）
 */

#include "pid_comm.h"

#include <string.h>

/* ============================ CRC16-Modbus ============================ */

uint16_t pid_crc16_modbus(const uint8_t *data, uint16_t len)
{
    uint16_t crc = 0xFFFFu;

    if (data == NULL) {
        return crc;
    }

    while (len-- > 0u) {
        crc ^= (uint16_t)(*data);
        data++;

        for (uint8_t bit = 0u; bit < 8u; bit++) {
            if ((crc & 0x0001u) != 0u) {
                crc = (uint16_t)((crc >> 1) ^ 0xA001u);
            } else {
                crc = (uint16_t)(crc >> 1);
            }
        }
    }

    return crc;
}

/* ============================ 小端读写 ============================ */

void pid_write_u16le(uint8_t *dst, uint16_t v)
{
    dst[0] = (uint8_t)(v & 0x00FFu);
    dst[1] = (uint8_t)((v >> 8) & 0x00FFu);
}

uint16_t pid_read_u16le(const uint8_t *src)
{
    return (uint16_t)(((uint16_t)src[0]) | ((uint16_t)src[1] << 8));
}

void pid_write_u32le(uint8_t *dst, uint32_t v)
{
    dst[0] = (uint8_t)(v & 0xFFu);
    dst[1] = (uint8_t)((v >> 8) & 0xFFu);
    dst[2] = (uint8_t)((v >> 16) & 0xFFu);
    dst[3] = (uint8_t)((v >> 24) & 0xFFu);
}

uint32_t pid_read_u32le(const uint8_t *src)
{
    return ((uint32_t)src[0])
         | ((uint32_t)src[1] << 8)
         | ((uint32_t)src[2] << 16)
         | ((uint32_t)src[3] << 24);
}

void pid_write_f32le(uint8_t *dst, float v)
{
    uint32_t bits = 0u;

    /* 先用 memcpy 取位模式，再按小端拆字节：不依赖编译器的 float 布局，
       也不依赖目标机字节序（大端 MCU 上同样正确）。 */
    memcpy(&bits, &v, sizeof(bits));
    pid_write_u32le(dst, bits);
}

float pid_read_f32le(const uint8_t *src)
{
    uint32_t bits = pid_read_u32le(src);
    float v = 0.0f;

    memcpy(&v, &bits, sizeof(v));
    return v;
}

/* ============================ 环形缓冲 ============================ */

void pid_rb_init(pid_rb_t *rb)
{
    if (rb == NULL) {
        return;
    }

    rb->head = 0u;
    rb->tail = 0u;
    rb->drop_count = 0u;
}

bool pid_rb_push(pid_rb_t *rb, uint8_t byte)
{
    uint16_t head;
    uint16_t tail;

    if (rb == NULL) {
        return false;
    }

    head = rb->head;
    tail = rb->tail;

    if ((uint16_t)(head - tail) >= (uint16_t)PID_RX_RING_SIZE) {
        /* 满：丢弃新字节（保持已经排队的旧数据完整） */
        rb->drop_count++;
        return false;
    }

    rb->buf[head & (uint16_t)(PID_RX_RING_SIZE - 1u)] = byte;
    rb->head = (uint16_t)(head + 1u);
    return true;
}

uint16_t pid_rb_count(const pid_rb_t *rb)
{
    if (rb == NULL) {
        return 0u;
    }

    return (uint16_t)(rb->head - rb->tail);
}

uint16_t pid_rb_read(pid_rb_t *rb, uint8_t *dst, uint16_t max)
{
    uint16_t tail;
    uint16_t head;
    uint16_t n = 0u;

    if ((rb == NULL) || (dst == NULL) || (max == 0u)) {
        return 0u;
    }

    tail = rb->tail;
    head = rb->head;

    while ((tail != head) && (n < max)) {
        dst[n] = rb->buf[tail & (uint16_t)(PID_RX_RING_SIZE - 1u)];
        tail++;
        n++;
    }

    rb->tail = tail; /* 一次性发布已消费位置 */
    return n;
}

/* ============================ 解析窗口工具 ============================ */

/** 丢弃窗口头部 n 字节（为重新同步让位） */
static void parser_drop(pid_parser_t *ps, uint16_t n)
{
    if (n >= ps->len) {
        ps->dropped_bytes += ps->len;
        ps->len = 0u;
        return;
    }

    memmove(ps->win, &ps->win[n], (size_t)(ps->len - n));
    ps->len = (uint16_t)(ps->len - n);
    ps->dropped_bytes += n;
}

void pid_parser_init(pid_parser_t *ps)
{
    if (ps == NULL) {
        return;
    }

    ps->len = 0u;
    ps->n_frames = 0u;
    ps->err_crc = 0u;
    ps->err_format = 0u;
    ps->dropped_bytes = 0u;
}

/**
 * @brief 尽量从窗口头部解出帧
 * @note  每次循环要么解出一帧，要么丢弃至少 1 字节，要么因数据不足退出，
 *        因此不会死循环（PC 侧对“AA 后跟非 55”的 livelock 在 MCU 侧不存在）
 */
static uint16_t parser_drain(pid_parser_t *ps, pid_frame_cb_t cb, void *user)
{
    uint16_t frames = 0u;

    while (ps->len > 0u) {
        uint16_t skip = 0u;
        uint16_t n;
        uint16_t total;
        uint16_t crc_rx;
        uint16_t crc_calc;
        pid_frame_t frame;

        /* 1) 快速跳过非 0xAA 字节（一次 memmove 丢掉一整段） */
        while ((skip < ps->len) && (ps->win[skip] != PID_SOF0)) {
            skip++;
        }
        if (skip > 0u) {
            parser_drop(ps, skip);
            continue;
        }

        /* 2) 帧头第二字节 */
        if (ps->len < 2u) {
            break;
        }
        if (ps->win[1] != PID_SOF1) {
            parser_drop(ps, 1u); /* 可能是 AA AA 55 的重叠帧头 */
            continue;
        }

        /* 3) LEN / TYPE */
        if (ps->len < 4u) {
            break;
        }
        n = ps->win[2];
        if (n > PID_MAX_PAYLOAD) {
            ps->err_format++;
            parser_drop(ps, 1u);
            continue;
        }

        total = (uint16_t)(n + PID_FRAME_OVERHEAD);
        if (ps->len < total) {
            break; /* 等后续字节 */
        }

        /* 4) 帧尾（按本帧自身尾部校验） */
        if ((ps->win[total - 4u] != PID_TAIL_BYTE) ||
            (ps->win[total - 3u] != PID_TAIL_BYTE) ||
            (ps->win[total - 2u] != PID_TAIL_BYTE) ||
            (ps->win[total - 1u] != PID_TAIL_BYTE)) {
            ps->err_format++;
            parser_drop(ps, 1u);
            continue;
        }

        /* 5) CRC：校验范围 LEN + TYPE + PAYLOAD */
        crc_rx = pid_read_u16le(&ps->win[4u + n]);
        crc_calc = pid_crc16_modbus(&ps->win[2], (uint16_t)(n + 2u));
        if (crc_rx != crc_calc) {
            ps->err_crc++;
            /* 与 PC 侧一致：整帧丢弃，不跨帧拼接 */
            parser_drop(ps, total);
            continue;
        }

        /* 6) 出帧 */
        frame.type = ps->win[3];
        frame.len = n;
        frame.payload = &ps->win[4];

        ps->n_frames++;
        frames++;

        if (cb != NULL) {
            cb(&frame, user);
        }

        parser_drop(ps, total);
    }

    return frames;
}

uint16_t pid_parser_feed(pid_parser_t *ps, const uint8_t *src, uint16_t len,
                         pid_frame_cb_t cb, void *user)
{
    uint16_t frames = 0u;

    if ((ps == NULL) || (src == NULL) || (len == 0u)) {
        return 0u;
    }

    for (uint16_t i = 0u; i < len; i++) {
        /* 窗口满且没有完整帧可解：丢掉最旧字节腾位置（只会在垃圾流时发生） */
        if (ps->len >= (uint16_t)PID_MAX_FRAME) {
            parser_drop(ps, 1u);
        }

        ps->win[ps->len] = src[i];
        ps->len++;

        frames += parser_drain(ps, cb, user);
    }

    return frames;
}

uint16_t pid_comm_poll(pid_parser_t *ps, pid_rb_t *rb,
                       pid_frame_cb_t cb, void *user)
{
    uint8_t chunk[32];
    uint16_t total = 0u;
    uint16_t n;

    if ((ps == NULL) || (rb == NULL)) {
        return 0u;
    }

    while ((n = pid_rb_read(rb, chunk, (uint16_t)sizeof(chunk))) > 0u) {
        total += pid_parser_feed(ps, chunk, n, cb, user);
    }

    return total;
}

/* ============================ 组帧 ============================ */

uint16_t pid_frame_encode(uint8_t *out, uint16_t cap, uint8_t type,
                          const uint8_t *payload, uint16_t payload_len)
{
    uint16_t total;
    uint16_t crc;

    if (out == NULL) {
        return 0u;
    }
    if (payload_len > PID_MAX_PAYLOAD) {
        return 0u;
    }
    if ((payload_len > 0u) && (payload == NULL)) {
        return 0u;
    }

    total = (uint16_t)(payload_len + PID_FRAME_OVERHEAD);
    if (cap < total) {
        return 0u;
    }

    out[0] = PID_SOF0;
    out[1] = PID_SOF1;
    out[2] = (uint8_t)payload_len;
    out[3] = type;

    if (payload_len > 0u) {
        memcpy(&out[4], payload, (size_t)payload_len);
    }

    crc = pid_crc16_modbus(&out[2], (uint16_t)(payload_len + 2u));
    pid_write_u16le(&out[4u + payload_len], crc);

    out[6u + payload_len] = PID_TAIL_BYTE;
    out[7u + payload_len] = PID_TAIL_BYTE;
    out[8u + payload_len] = PID_TAIL_BYTE;
    out[9u + payload_len] = PID_TAIL_BYTE;

    return total;
}

uint16_t pid_build_pong(uint8_t *out, uint16_t cap,
                        uint8_t fw_ver, uint8_t status)
{
    uint8_t pl[2];

    pl[0] = fw_ver;
    pl[1] = status;

    return pid_frame_encode(out, cap, (uint8_t)PID_PKT_PONG, pl, 2u);
}

uint16_t pid_build_pid_ack(uint8_t *out, uint16_t cap,
                           float kp, float ki, float kd, float sp,
                           uint8_t result)
{
    uint8_t pl[17];

    pid_write_f32le(&pl[0], kp);
    pid_write_f32le(&pl[4], ki);
    pid_write_f32le(&pl[8], kd);
    pid_write_f32le(&pl[12], sp);
    pl[16] = result;

    return pid_frame_encode(out, cap, (uint8_t)PID_PKT_PID_ACK, pl, 17u);
}

uint16_t pid_build_telemetry(uint8_t *out, uint16_t cap,
                             uint32_t t_ms, float sp, float pv,
                             float out_val, float err)
{
    uint8_t pl[20];

    pid_write_u32le(&pl[0], t_ms);
    pid_write_f32le(&pl[4], sp);
    pid_write_f32le(&pl[8], pv);
    pid_write_f32le(&pl[12], out_val);
    pid_write_f32le(&pl[16], err);

    return pid_frame_encode(out, cap, (uint8_t)PID_PKT_TELEMETRY, pl, 20u);
}

uint16_t pid_build_step_event(uint8_t *out, uint16_t cap,
                              uint8_t phase, uint32_t t_ms)
{
    uint8_t pl[5];

    pl[0] = phase;
    pid_write_u32le(&pl[1], t_ms);

    return pid_frame_encode(out, cap, (uint8_t)PID_PKT_STEP_EVENT, pl, 5u);
}

uint16_t pid_build_error(uint8_t *out, uint16_t cap,
                         uint8_t code, const char *msg)
{
    uint8_t pl[PID_ERROR_MSG_MAX + 2u];
    uint16_t msg_len = 0u;

    if (msg != NULL) {
        while ((msg[msg_len] != '\0') && (msg_len < PID_ERROR_MSG_MAX)) {
            pl[2u + msg_len] = (uint8_t)msg[msg_len];
            msg_len++;
        }
    }

    pl[0] = code;
    pl[1] = (uint8_t)msg_len;

    return pid_frame_encode(out, cap, (uint8_t)PID_PKT_ERROR, pl,
                            (uint16_t)(msg_len + 2u));
}
