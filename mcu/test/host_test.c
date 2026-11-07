/**
 * @file    host_test.c
 * @brief   MCU 协议层 / PID 层主机侧离线测试（MinGW / Linux gcc 均可编译）
 *
 * 不依赖 HAL：只编译 pid_comm.c 与 pid_calc.c，黄金真值来自
 * mcu/test/golden_vectors.h（由 PC 侧冻结协议 protocol.py 生成）。
 *
 * 编译（Windows MinGW）：
 *   gcc -std=c11 -Wall -Wextra -I../Inc -I. host_test.c ../Src/pid_comm.c \
 *       ../Src/pid_calc.c -o host_test.exe
 * 运行：
 *   ./host_test.exe
 *
 * 退出码 0 = 全部通过；非 0 = 失败计数。
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "pid_comm.h"
#include "pid_calc.h"
#include "golden_vectors.h"

static int g_failures = 0;

#define CHECK(cond, msg) do { \
    if (cond) { \
        printf("  PASS %s\n", msg); \
    } else { \
        printf("  FAIL %s (line %d)\n", msg, __LINE__); \
        g_failures++; \
    } \
} while (0)

/* ------------------------------------------------------------------ */
/* 1. CRC 黄金向量                                                     */
/* ------------------------------------------------------------------ */

static void test_crc(void)
{
    printf("[CRC]\n");
    for (uint16_t i = 0u; i < VEC_CRC_COUNT; i++) {
        uint16_t got = pid_crc16_modbus(VEC_CRC[i].data, VEC_CRC[i].len);
        char buf[64];
        snprintf(buf, sizeof(buf), "crc[%u] = 0x%04X", i, VEC_CRC[i].crc);
        CHECK(got == VEC_CRC[i].crc, buf);
    }
}

/* ------------------------------------------------------------------ */
/* 2. 解析黄金向量（按 chunk 分片喂入，覆盖粘包/拆包/重同步）          */
/* ------------------------------------------------------------------ */

static uint8_t  g_type[64];
static uint16_t g_len[64];
static uint8_t  g_payload[64][250];
static uint16_t g_n;

static void collect_cb(const pid_frame_t *frame, void *user)
{
    (void)user;
    if (g_n >= 64u) {
        return;
    }
    g_type[g_n] = frame->type;
    g_len[g_n] = frame->len;
    if (frame->len > 0u) {
        memcpy(g_payload[g_n], frame->payload, frame->len);
    }
    g_n++;
}

static void run_case(const vec_case_t *v)
{
    pid_parser_t parser;
    pid_parser_init(&parser);
    g_n = 0u;

    uint16_t off = 0u;
    while (off < v->stream_len) {
        uint16_t chunk = v->chunk;
        if (chunk > (v->stream_len - off)) {
            chunk = (uint16_t)(v->stream_len - off);
        }
        (void)pid_parser_feed(&parser, &v->stream[off], chunk, collect_cb, NULL);
        off += chunk;
    }

    char msg[64];
    snprintf(msg, sizeof(msg), "%s: frame count=%u expect=%u",
             v->name, g_n, v->frame_count);
    CHECK(g_n == v->frame_count, msg);

    for (uint16_t i = 0u; i < g_n && i < v->frame_count; i++) {
        const vec_frame_t *expect = &v->frames[i];
        snprintf(msg, sizeof(msg), "%s: frame[%u] type", v->name, i);
        CHECK(g_type[i] == expect->type, msg);

        snprintf(msg, sizeof(msg), "%s: frame[%u] len", v->name, i);
        CHECK(g_len[i] == expect->len, msg);

        if (expect->len > 0u) {
            snprintf(msg, sizeof(msg), "%s: frame[%u] payload", v->name, i);
            CHECK(memcmp(g_payload[i], expect->payload, expect->len) == 0, msg);
        }
    }
}

static void test_parser(void)
{
    printf("[Parser cases]\n");
    for (uint16_t c = 0u; c < VEC_CASE_COUNT; c++) {
        run_case(&VEC_CASES[c]);
    }
}

/* ------------------------------------------------------------------ */
/* 3. 组帧黄金向量（逐字节比对）                                       */
/* ------------------------------------------------------------------ */

static void test_encode(void)
{
    printf("[Encode]\n");
    uint8_t out[PID_MAX_FRAME];
    uint16_t n;

    n = pid_build_pong(out, sizeof(out), 1u, 0u);
    CHECK(n == VEC_ENC_PONG_LEN && memcmp(out, ENC_PONG, n) == 0, "PONG 帧");

    n = pid_build_pid_ack(out, sizeof(out), 2.0f, 0.5f, 0.1f, 30.0f, 0u);
    CHECK(n == VEC_ENC_ACK_LEN && memcmp(out, ENC_ACK, n) == 0, "PID_ACK 帧");

    n = pid_build_telemetry(out, sizeof(out), 1000u, 25.0f, 24.5f, 60.0f, -0.5f);
    CHECK(n == VEC_ENC_TEL_LEN && memcmp(out, ENC_TEL, n) == 0, "TELEMETRY 帧");

    n = pid_build_step_event(out, sizeof(out), 1u, 2000u);
    CHECK(n == VEC_ENC_EVT_LEN && memcmp(out, ENC_EVT, n) == 0, "STEP_EVENT 帧");

    n = pid_build_error(out, sizeof(out), 1u, "bad param");
    CHECK(n == VEC_ENC_ERR_LEN && memcmp(out, ENC_ERR, n) == 0, "ERROR 帧");
}

/* ------------------------------------------------------------------ */
/* 4. PID 基本行为：限幅、有限性、闭环收敛方向                         */
/* ------------------------------------------------------------------ */

static void test_pid(void)
{
    printf("[PID]\n");
    pid_cfg_t cfg;
    pid_cfg_default(&cfg);
    cfg.kp = 4.0f;
    cfg.ki = 0.2f;
    cfg.kd = 0.05f;
    cfg.out_min = 0.0f;
    cfg.out_max = 100.0f;
    cfg.dt = 0.01f;

    pid_ctrl_t pid;
    pid_init(&pid, &cfg);

    float pv = 0.0f;
    float sp = 30.0f;
    bool  saturated_high = false;
    bool  finite_ok = true;

    for (int i = 0; i < 500; i++) {
        float out = pid_update_dt(&pid, sp, pv, cfg.dt);
        if (!pid_float_is_finite(out)) {
            finite_ok = false;
        }
        if (out > 100.0f || out < 0.0f) {
            g_failures++;
            printf("  FAIL 输出越界: %.3f\n", out);
        }
        if (out >= 100.0f) {
            saturated_high = true;
        }
        /* 一阶惯性植物：输出推动 PV 朝设定值靠近 */
        pv += (sp - pv) * 0.01f + out * 0.002f;
    }

    CHECK(finite_ok, "PID 输出全程有限");
    CHECK(saturated_high, "大偏差阶段输出曾顶到上限（抗饱和路径被覆盖）");
    CHECK(pv > 20.0f, "闭环后 PV 向设定值靠近");
    CHECK(pid.out >= cfg.out_min && pid.out <= cfg.out_max, "最终输出在限幅内");
}

int main(void)
{
    test_crc();
    test_parser();
    test_encode();
    test_pid();

    if (g_failures == 0) {
        printf("\nALL HOST TESTS PASSED\n");
        return 0;
    }
    printf("\n%d FAILURE(S)\n", g_failures);
    return 1;
}
