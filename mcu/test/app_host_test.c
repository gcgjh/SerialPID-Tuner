/**
 * @file    app_host_test.c
 * @brief   应用层主机侧冒烟测试：注入 mock IO，走通 PING/PID_SET/阶跃/遥测。
 *
 * 编译（与 host_test 同目录）：
 *   gcc -std=c11 -Wall -Wextra -I../Inc -I. app_host_test.c \
 *       ../Src/pid_comm.c ../Src/pid_calc.c ../Src/pid_app.c -o app_host_test.exe
 */

#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>

#include "pid_app.h"

static int g_failures = 0;

#define CHECK(cond, msg) do { \
    if (cond) { printf("  PASS %s\n", msg); } \
    else { printf("  FAIL %s (line %d)\n", msg, __LINE__); g_failures++; } \
} while (0)

/* ---------------- mock 平台 ---------------- */

typedef struct {
    uint8_t  tx[4096];
    uint16_t tx_len;
    float    pv;
    float    out;
} mock_t;

static mock_t g_mock;

static bool mock_send(void *ctx, const uint8_t *data, uint16_t len)
{
    mock_t *m = (mock_t *)ctx;
    if ((uint16_t)(m->tx_len + len) > sizeof(m->tx)) {
        return false;
    }
    memcpy(&m->tx[m->tx_len], data, len);
    m->tx_len += len;
    return true;
}

static float mock_read_pv(void *ctx)
{
    return ((mock_t *)ctx)->pv;
}

static void mock_write_out(void *ctx, float out)
{
    ((mock_t *)ctx)->out = out;
}

/* ---------------- 帧收集 ---------------- */

static uint8_t  g_type[32];
static uint16_t g_len[32];
static uint8_t  g_pl[32][250];
static uint16_t g_n;

static void collect(const pid_frame_t *f, void *user)
{
    (void)user;
    if (g_n >= 32u) {
        return;
    }
    g_type[g_n] = f->type;
    g_len[g_n] = f->len;
    if (f->len > 0u) {
        memcpy(g_pl[g_n], f->payload, f->len);
    }
    g_n++;
}

static void drain_tx(pid_app_t *app)
{
    pid_parser_t ps;
    pid_parser_init(&ps);
    g_n = 0u;
    (void)pid_parser_feed(&ps, g_mock.tx, g_mock.tx_len, collect, NULL);
    g_mock.tx_len = 0u;
    (void)app;
}

static void feed(pid_app_t *app, const uint8_t *data, uint16_t len)
{
    pid_app_on_rx_block(app, data, len);
    pid_app_poll(app);
}

static bool f32_close(float a, float b)
{
    float d = a - b;
    if (d < 0.0f) { d = -d; }
    return d < 1e-5f;
}

int main(void)
{
    pid_app_io_t io = {
        .write_output = mock_write_out,
        .read_pv = mock_read_pv,
        .send_frame = mock_send,
        .nvm_load = NULL,
        .nvm_save = NULL,
        .system_reset = NULL,
        .ctx = &g_mock,
    };

    pid_app_t app;
    pid_app_init(&app, &io, NULL);

    uint8_t  buf[PID_MAX_FRAME];
    uint16_t n;

    /* 1) PING -> PONG */
    printf("[App: PING/PONG]\n");
    n = pid_frame_encode(buf, sizeof(buf), PID_PKT_PING, NULL, 0u);
    feed(&app, buf, n);
    drain_tx(&app);
    CHECK(g_n == 1u, "PING 回一帧 PONG");
    CHECK(g_n == 1u && g_type[0] == PID_PKT_PONG, "PONG 类型 0x02");
    CHECK(g_n == 1u && g_len[0] == 2u, "PONG 载荷 2 字节");
    CHECK(g_n == 1u && g_pl[0][1] == PID_STATUS_RUNNING, "PONG.status=RUNNING");

    /* 2) PID_SET -> PID_ACK(result=0, 回显参数) */
    printf("[App: PID_SET/ACK]\n");
    uint8_t pl[16];
    pid_write_f32le(&pl[0], 2.0f);
    pid_write_f32le(&pl[4], 0.5f);
    pid_write_f32le(&pl[8], 0.1f);
    pid_write_f32le(&pl[12], 30.0f);
    n = pid_frame_encode(buf, sizeof(buf), PID_PKT_PID_SET, pl, 16u);
    feed(&app, buf, n);
    drain_tx(&app);
    CHECK(g_n == 1u && g_type[0] == PID_PKT_PID_ACK, "ACK 类型 0x11");
    CHECK(g_n == 1u && g_len[0] == 17u, "ACK 载荷 17 字节");
    CHECK(g_n == 1u && g_pl[0][16] == PID_ACK_OK, "ACK.result=0 接受");
    if (g_n == 1u) {
        CHECK(f32_close(pid_read_f32le(&g_pl[0][0]), 2.0f), "ACK 回显 Kp");
        CHECK(f32_close(pid_read_f32le(&g_pl[0][4]), 0.5f), "ACK 回显 Ki");
        CHECK(f32_close(pid_read_f32le(&g_pl[0][8]), 0.1f), "ACK 回显 Kd");
        CHECK(f32_close(pid_read_f32le(&g_pl[0][12]), 30.0f), "ACK 回显 SP");
    }

    /* 3) STEP_START -> STEP_EVENT(RUNNING) */
    printf("[App: STEP_START]\n");
    uint8_t spl[8];
    pid_write_f32le(&spl[0], 5.0f);
    pid_write_u32le(&spl[4], 1000u);
    n = pid_frame_encode(buf, sizeof(buf), PID_PKT_STEP_START, spl, 8u);
    feed(&app, buf, n);
    drain_tx(&app);
    CHECK(g_n == 1u && g_type[0] == PID_PKT_STEP_EVENT, "EVENT 类型 0x32");
    CHECK(g_n == 1u && g_pl[0][0] == PID_PHASE_RUNNING, "EVENT.phase=RUNNING");

    /* 4) 跑 1.5s 节拍：阶跃保持结束应回 DONE，并持续上报遥测 */
    printf("[App: tick / telemetry / step done]\n");
    bool saw_done = false;
    uint16_t tel_count = 0u;
    for (uint32_t t = 50u; t <= 1500u; t += 50u) {
        pid_app_tick(&app, t);
        /* 简易一阶植物 */
        g_mock.pv += (app.sp - g_mock.pv) * 0.02f + g_mock.out * 0.002f;
        drain_tx(&app);
        for (uint16_t i = 0u; i < g_n; i++) {
            if (g_type[i] == PID_PKT_STEP_EVENT && g_pl[i][0] == PID_PHASE_DONE) {
                saw_done = true;
            }
            if (g_type[i] == PID_PKT_TELEMETRY) {
                tel_count++;
            }
        }
    }
    CHECK(saw_done, "保持结束后回 STEP_EVENT(DONE)");
    CHECK(tel_count > 0u, "节拍期间上报了遥测帧");
    CHECK(app.step_active == false, "阶跃结束后退出开环");

    /* 5) 非法参数：NaN Kp 应回 BAD_PARAM，不改原参数 */
    printf("[App: bad param]\n");
    uint8_t bad[16];
    pid_write_f32le(&bad[0], 0.0f / 0.0f); /* NaN */
    pid_write_f32le(&bad[4], 0.5f);
    pid_write_f32le(&bad[8], 0.1f);
    pid_write_f32le(&bad[12], 30.0f);
    n = pid_frame_encode(buf, sizeof(buf), PID_PKT_PID_SET, bad, 16u);
    feed(&app, buf, n);
    drain_tx(&app);
    CHECK(g_n == 1u && g_pl[0][16] == PID_ACK_BAD_PARAM, "NaN 参数回 BAD_PARAM");

    if (g_failures == 0) {
        printf("\nALL APP HOST TESTS PASSED\n");
        return 0;
    }
    printf("\n%d FAILURE(S)\n", g_failures);
    return 1;
}
