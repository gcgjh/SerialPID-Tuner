/**
 * @file    pid_app.c
 * @brief   PID 应用层实现（无 HAL 依赖）
 *
 * 线程模型：
 *   - pid_app_on_rx_byte   ：UART 中断上下文，只写环形缓冲
 *   - pid_app_poll / tick  ：主循环上下文，唯一消费者，无并发写
 *   因此除 rb 的 head/tail 外，其余状态都无需临界区。
 */

#include "pid_app.h"

#include <stddef.h>
#include <string.h>

/* ============================ 内部工具 ============================ */

static float app_clamp(float v, float lo, float hi)
{
    if (v > hi) {
        return hi;
    }
    if (v < lo) {
        return lo;
    }
    return v;
}

static float app_abs(float v)
{
    return (v < 0.0f) ? -v : v;
}

static uint16_t nvm_crc(const pid_nvm_t *n)
{
    return pid_crc16_modbus((const uint8_t *)n,
                            (uint16_t)offsetof(pid_nvm_t, crc));
}

static void nvm_fill(pid_app_t *app, pid_nvm_t *n)
{
    memset(n, 0, sizeof(*n));
    n->magic = PID_NVM_MAGIC;
    n->kp = app->pid.cfg.kp;
    n->ki = app->pid.cfg.ki;
    n->kd = app->pid.cfg.kd;
    n->sp = app->sp;
    n->mode = (uint32_t)app->pid.cfg.mode;
    n->crc = nvm_crc(n);
}

static bool nvm_valid(const pid_nvm_t *n)
{
    if (n->magic != PID_NVM_MAGIC) {
        return false;
    }
    if (n->crc != nvm_crc(n)) {
        return false;
    }
    if (!pid_float_is_finite(n->kp) || !pid_float_is_finite(n->ki) ||
        !pid_float_is_finite(n->kd) || !pid_float_is_finite(n->sp)) {
        return false;
    }
    if ((n->mode != (uint32_t)PID_MODE_POSITIONAL) &&
        (n->mode != (uint32_t)PID_MODE_INCREMENTAL)) {
        return false;
    }
    return true;
}

/* ============================ 发送 ============================ */

static void app_send_raw(pid_app_t *app, const uint8_t *buf, uint16_t len)
{
    if ((len == 0u) || (app->io.send_frame == NULL)) {
        return;
    }

    if (app->io.send_frame(app->io.ctx, buf, len)) {
        app->tx_frames++;
        app->tx_bytes += len;
    }
}

static void app_send_error(pid_app_t *app, uint8_t code, const char *msg)
{
    uint8_t  out[PID_MAX_FRAME];
    uint16_t n = pid_build_error(out, (uint16_t)sizeof(out), code, msg);

    app_send_raw(app, out, n);
}

static uint8_t app_status(const pid_app_t *app)
{
    /* 与冻结协议一致：0 空闲 / 1 运行 / 2 阶跃中；NVM 故障走 ERROR 帧上报，不占 status 位 */
    return app->step_active ? PID_STATUS_STEPPING : PID_STATUS_RUNNING;
}

static void app_send_pong(pid_app_t *app)
{
    uint8_t  out[PID_MAX_FRAME];
    uint16_t n = pid_build_pong(out, (uint16_t)sizeof(out),
                                (uint8_t)PID_FW_VERSION, app_status(app));

    app_send_raw(app, out, n);
}

static void app_send_step_event(pid_app_t *app, uint8_t phase, uint32_t t_ms)
{
    uint8_t  out[PID_MAX_FRAME];
    uint16_t n = pid_build_step_event(out, (uint16_t)sizeof(out), phase, t_ms);

    app_send_raw(app, out, n);
}

/* ============================ 参数变更 ============================ */

static void app_mark_dirty(pid_app_t *app)
{
    if (app->io.nvm_save == NULL) {
        return;
    }

    app->nvm_dirty = true;
    app->nvm_dirty_ms = app->now_ms;
}

/* ============================ 阶跃辨识 ============================ */

static void app_step_finish(pid_app_t *app, uint32_t now_ms, uint8_t phase)
{
    uint32_t elapsed;

    if (!app->step_active) {
        return;
    }

    elapsed = now_ms - app->step_t0;

    app->step_active = false;
    app->out = app->step_base;
    if (app->io.write_output != NULL) {
        app->io.write_output(app->io.ctx, app->out);
    }

    /* 无扰回到闭环：反算积分，避免恢复瞬间输出跳变 */
    pid_track(&app->pid, app->sp, app->pv, app->out);

    app_send_step_event(app, phase, elapsed);
}

/* ============================ 单拍控制 ============================ */

static void app_run_control(pid_app_t *app, uint32_t now_ms)
{
    uint32_t elapsed = now_ms - app->t_ctrl;
    float    dt;

    if (elapsed < PID_CTRL_PERIOD_MS) {
        return;
    }

    /* 用真实经过时间算 dt；单次跳变超过 5 个周期时用名义值兜底 */
    dt = (float)elapsed / 1000.0f;
    if (dt > (5.0f * (float)PID_CTRL_PERIOD_MS / 1000.0f)) {
        dt = (float)PID_CTRL_PERIOD_MS / 1000.0f;
    }

    app->t_ctrl = now_ms;

    if (app->step_active) {
        uint32_t step_elapsed = now_ms - app->step_t0;

        if (step_elapsed >= app->step_hold) {
            app_step_finish(app, now_ms, PID_PHASE_DONE);  /* 保持结束 → 完成 */
            return;
        }

        app->out = app_clamp(app->step_base + app->step_amp,
                             app->pid.cfg.out_min, app->pid.cfg.out_max);
        if (app->io.write_output != NULL) {
            app->io.write_output(app->io.ctx, app->out);
        }
        return;   /* 开环期间冻结 PID 状态 */
    }

    app->err = app->sp - app->pv;
    app->out = pid_update_dt(&app->pid, app->sp, app->pv, dt);

    if (app->io.write_output != NULL) {
        app->io.write_output(app->io.ctx, app->out);
    }
}

/* ============================ 帧处理 ============================ */

static void app_handle_pid_set(pid_app_t *app, const pid_frame_t *f)
{
    float kp;
    float ki;
    float kd;
    float sp;

    if (f->len != 16u) {
        app_send_error(app, (uint8_t)PID_ERR_PAYLOAD, "PID_SET needs 16 bytes");
        return;
    }

    kp = pid_read_f32le(&f->payload[0]);
    ki = pid_read_f32le(&f->payload[4]);
    kd = pid_read_f32le(&f->payload[8]);
    sp = pid_read_f32le(&f->payload[12]);

    if (!pid_float_is_finite(kp) || !pid_float_is_finite(ki) ||
        !pid_float_is_finite(kd) || !pid_float_is_finite(sp)) {
        uint8_t out[PID_MAX_FRAME];
        uint16_t n = pid_build_pid_ack(out, (uint16_t)sizeof(out),
                                       app->pid.cfg.kp, app->pid.cfg.ki,
                                       app->pid.cfg.kd, app->sp,
                                       (uint8_t)PID_ACK_BAD_PARAM);
        app_send_raw(app, out, n);
        return;
    }

    pid_set_gains(&app->pid, kp, ki, kd);
    app->sp = sp;
    app_mark_dirty(app);

    {
        uint8_t  out[PID_MAX_FRAME];
        uint16_t n = pid_build_pid_ack(out, (uint16_t)sizeof(out),
                                       app->pid.cfg.kp, app->pid.cfg.ki,
                                       app->pid.cfg.kd, app->sp,
                                       (uint8_t)PID_ACK_OK);
        app_send_raw(app, out, n);
    }
}

static void app_handle_sp_set(pid_app_t *app, const pid_frame_t *f)
{
    float sp;

    if (f->len != 4u) {
        app_send_error(app, (uint8_t)PID_ERR_PAYLOAD, "SP_SET needs 4 bytes");
        return;
    }

    sp = pid_read_f32le(f->payload);
    if (!pid_float_is_finite(sp)) {
        app_send_error(app, (uint8_t)PID_ERR_PARAM, "SP is NaN/Inf");
        return;
    }

    app->sp = sp;
    app_mark_dirty(app);
    /* 协议未定义 SP_SET 的应答；PID_ACK 仅用于 0x10 */
}

static void app_handle_step_start(pid_app_t *app, const pid_frame_t *f)
{
    float    amp;
    uint32_t hold;

    if (f->len != 8u) {
        app_send_error(app, (uint8_t)PID_ERR_PAYLOAD, "STEP_START needs 8 bytes");
        return;
    }

    amp = pid_read_f32le(&f->payload[0]);
    hold = pid_read_u32le(&f->payload[4]);

    if (!pid_float_is_finite(amp) || (app_abs(amp) > PID_STEP_MAX_AMP) ||
        (hold == 0u) || (hold > PID_STEP_MAX_MS)) {
        app_send_error(app, (uint8_t)PID_ERR_STEP, "STEP param out of range");
        return;
    }

    /* 重复下发视为以新参数重启本次测试 */
    if (app->step_active) {
        app_step_finish(app, app->now_ms, PID_PHASE_IDLE);  /* 中止/停止 → 空闲 */
    }

    app->step_base = app->out;
    app->step_amp = amp;
    app->step_hold = hold;
    app->step_t0 = app->now_ms;
    app->step_active = true;

    app_send_step_event(app, PID_PHASE_RUNNING, app->now_ms);  /* 开始 → 运行中 */
}

static void app_handle_reset(pid_app_t *app)
{
    if (app->io.system_reset != NULL) {
        app->io.system_reset(app->io.ctx);
        return;
    }

    /* 没有硬复位钩子时做软复位：回到安全状态并回 PONG */
    app->step_active = false;
    app->out = app->pid.cfg.out_min;
    pid_reset(&app->pid, app->out);
    if (app->io.write_output != NULL) {
        app->io.write_output(app->io.ctx, app->out);
    }
    app_send_pong(app);
}

static void app_on_frame(const pid_frame_t *f, void *user)
{
    pid_app_t *app = (pid_app_t *)user;

    if ((app == NULL) || (f == NULL)) {
        return;
    }

    app->rx_frames++;

    switch (f->type) {
    case (uint8_t)PID_PKT_PING:
        app_send_pong(app);
        break;

    case (uint8_t)PID_PKT_PID_SET:
        app_handle_pid_set(app, f);
        break;

    case (uint8_t)PID_PKT_SP_SET:
        app_handle_sp_set(app, f);
        break;

    case (uint8_t)PID_PKT_STEP_START:
        app_handle_step_start(app, f);
        break;

    case (uint8_t)PID_PKT_STEP_STOP:
        app_step_finish(app, app->now_ms, PID_PHASE_IDLE);  /* 中止/停止 → 空闲 */
        break;

    case (uint8_t)PID_PKT_RESET:
        app_handle_reset(app);
        break;

    /* 下行不该出现的类型：静默忽略，避免 PC 侧被噪声包刷屏 */
    case (uint8_t)PID_PKT_PONG:
    case (uint8_t)PID_PKT_PID_ACK:
    case (uint8_t)PID_PKT_TELEMETRY:
    case (uint8_t)PID_PKT_STEP_EVENT:
    case (uint8_t)PID_PKT_ERROR:
        break;

    default:
        app_send_error(app, (uint8_t)PID_ERR_TYPE, "unknown type");
        break;
    }
}

/* ============================ 生命周期 ============================ */

void pid_app_init(pid_app_t *app, const pid_app_io_t *io, const pid_cfg_t *cfg)
{
    pid_cfg_t base;
    pid_nvm_t nvm;

    if (app == NULL) {
        return;
    }

    memset(app, 0, sizeof(*app));
    pid_rb_init(&app->rb);
    pid_parser_init(&app->parser);

    if (io != NULL) {
        app->io = *io;
    } else {
        memset(&app->io, 0, sizeof(app->io));
    }

    if (cfg != NULL) {
        base = *cfg;
    } else {
        pid_cfg_default(&base);
        base.mode = PID_DEFAULT_MODE;
    }

    pid_init(&app->pid, &base);
    app->out = app->pid.out;
    app->err = 0.0f;
    app->sp = 0.0f;
    app->pv = 0.0f;

    /* 参数优先从 Flash 恢复；CRC/魔数不符则保持默认 */
    if ((app->io.nvm_load != NULL) && app->io.nvm_load(app->io.ctx, &nvm) &&
        nvm_valid(&nvm)) {
        pid_set_gains(&app->pid, nvm.kp, nvm.ki, nvm.kd);
        pid_set_mode(&app->pid, (pid_mode_t)nvm.mode);
        app->sp = nvm.sp;
    }
}

void pid_app_on_rx_byte(pid_app_t *app, uint8_t byte)
{
    if (app == NULL) {
        return;
    }

    (void)pid_rb_push(&app->rb, byte);   /* 满则丢弃并计数，中断里不做别的 */
}

void pid_app_on_rx_block(pid_app_t *app, const uint8_t *data, uint16_t len)
{
    if ((app == NULL) || (data == NULL)) {
        return;
    }

    for (uint16_t i = 0u; i < len; i++) {
        (void)pid_rb_push(&app->rb, data[i]);
    }
}

void pid_app_poll(pid_app_t *app)
{
    if (app == NULL) {
        return;
    }

    (void)pid_comm_poll(&app->parser, &app->rb, app_on_frame, app);

    app->drop_bytes = app->rb.drop_count + app->parser.dropped_bytes;
}

void pid_app_tick(pid_app_t *app, uint32_t now_ms)
{
    if (app == NULL) {
        return;
    }

    app->now_ms = now_ms;

    if (app->io.read_pv != NULL) {
        float pv = app->io.read_pv(app->io.ctx);

        if (pid_float_is_finite(pv)) {
            app->pv = pv;
        }
    }

    app->err = app->sp - app->pv;

    app_run_control(app, now_ms);

    /* 遥测：周期上报；阶跃期间同样上报，PC 才能抓到响应曲线 */
    if ((uint32_t)(now_ms - app->t_tel) >= PID_TELEMETRY_PERIOD_MS) {
        uint8_t  out[PID_MAX_FRAME];
        uint16_t n;

        app->t_tel = now_ms;
        n = pid_build_telemetry(out, (uint16_t)sizeof(out), now_ms,
                                app->sp, app->pv, app->out, app->err);
        app_send_raw(app, out, n);
    }

    /* 参数静默 PID_NVM_SAVE_DELAY_MS 后才落 Flash，避免滑杆拖动磨损 Flash */
    if (app->nvm_dirty &&
        ((uint32_t)(now_ms - app->nvm_dirty_ms) >= PID_NVM_SAVE_DELAY_MS)) {
        pid_nvm_t n;

        nvm_fill(app, &n);
        app->nvm_dirty = false;

        if (app->io.nvm_save == NULL) {
            app->nvm_fail = false;
        } else if (!app->io.nvm_save(app->io.ctx, &n)) {
            app->nvm_fail = true;
            app_send_error(app, (uint8_t)PID_ERR_NVM, "flash save failed");
        } else {
            app->nvm_fail = false;
        }
    }
}

void pid_app_stop(pid_app_t *app)
{
    if (app == NULL) {
        return;
    }

    app->step_active = false;
    app->out = app->pid.cfg.out_min;
    app->err = app->sp - app->pv;

    if (app->io.write_output != NULL) {
        app->io.write_output(app->io.ctx, app->out);
    }

    pid_reset(&app->pid, app->out);
}
