/**
 * @file    pid_calc.c
 * @brief   离散 PID 算法实现（无 HAL / 无 libm 依赖）
 */

#include "pid_calc.h"

#include <float.h>
#include <string.h>

/* ============================ 内部工具 ============================ */

static float clampf_(float v, float lo, float hi)
{
    if (v > hi) {
        return hi;
    }
    if (v < lo) {
        return lo;
    }
    return v;
}

static float absf_(float v)
{
    return (v < 0.0f) ? -v : v;
}

/** NaN 与 ±Inf 都返回 false；不引 <math.h>，避免部分工程缺 libm */
bool pid_float_is_finite(float v)
{
    if (v != v) {           /* NaN */
        return false;
    }
    if ((v > FLT_MAX) || (v < -FLT_MAX)) { /* ±Inf */
        return false;
    }
    return true;
}

static bool finite_(float v)
{
    return pid_float_is_finite(v);
}

static void sort_limits_(float *lo, float *hi)
{
    if (*lo > *hi) {
        float t = *lo;
        *lo = *hi;
        *hi = t;
    }
}

/* ============================ 配置与初始化 ============================ */

void pid_cfg_default(pid_cfg_t *cfg)
{
    if (cfg == NULL) {
        return;
    }

    cfg->kp = 1.0f;
    cfg->ki = 0.0f;
    cfg->kd = 0.0f;
    cfg->out_min = 0.0f;
    cfg->out_max = 100.0f;
    cfg->dt = 0.01f;
    cfg->mode = PID_MODE_POSITIONAL;
    cfg->d_on_pv = true;
    cfg->d_lpf_tau = 0.0f;
    cfg->i_sep_err = 0.0f;
    cfg->i_clamp = true;
}

void pid_init(pid_ctrl_t *p, const pid_cfg_t *cfg)
{
    if (p == NULL) {
        return;
    }

    memset(p, 0, sizeof(*p));

    if (cfg != NULL) {
        p->cfg = *cfg;
    } else {
        pid_cfg_default(&p->cfg);
    }

    sort_limits_(&p->cfg.out_min, &p->cfg.out_max);

    if (!finite_(p->cfg.dt) || (p->cfg.dt <= 0.0f)) {
        p->cfg.dt = 0.01f;
    }
    if (!finite_(p->cfg.d_lpf_tau) || (p->cfg.d_lpf_tau < 0.0f)) {
        p->cfg.d_lpf_tau = 0.0f;
    }
    if (!finite_(p->cfg.i_sep_err) || (p->cfg.i_sep_err < 0.0f)) {
        p->cfg.i_sep_err = 0.0f;
    }
    if (!finite_(p->cfg.kp)) {
        p->cfg.kp = 0.0f;
    }
    if (!finite_(p->cfg.ki)) {
        p->cfg.ki = 0.0f;
    }
    if (!finite_(p->cfg.kd)) {
        p->cfg.kd = 0.0f;
    }

    p->inited = false;
    p->out = clampf_(0.0f, p->cfg.out_min, p->cfg.out_max);
    p->out_raw = p->out;
}

void pid_reset(pid_ctrl_t *p, float out_init)
{
    if (p == NULL) {
        return;
    }

    p->integ = 0.0f;
    p->prev_err = 0.0f;
    p->prev_pv = 0.0f;
    p->d_state = 0.0f;
    p->d_state_prev = 0.0f;
    p->inited = false;

    if (!finite_(out_init)) {
        out_init = 0.0f;
    }
    p->out = clampf_(out_init, p->cfg.out_min, p->cfg.out_max);
    p->out_raw = p->out;
}

void pid_set_gains(pid_ctrl_t *p, float kp, float ki, float kd)
{
    if (p == NULL) {
        return;
    }

    if (finite_(kp)) {
        p->cfg.kp = kp;
    }
    if (finite_(ki)) {
        p->cfg.ki = ki;
    }
    if (finite_(kd)) {
        p->cfg.kd = kd;
    }
}

void pid_set_limits(pid_ctrl_t *p, float out_min, float out_max)
{
    if ((p == NULL) || !finite_(out_min) || !finite_(out_max)) {
        return;
    }

    p->cfg.out_min = out_min;
    p->cfg.out_max = out_max;
    sort_limits_(&p->cfg.out_min, &p->cfg.out_max);

    p->out = clampf_(p->out, p->cfg.out_min, p->cfg.out_max);
    p->out_raw = clampf_(p->out_raw, p->cfg.out_min, p->cfg.out_max);
}

void pid_set_dt(pid_ctrl_t *p, float dt)
{
    if ((p == NULL) || !finite_(dt) || (dt <= 0.0f)) {
        return;
    }

    p->cfg.dt = dt;
}

void pid_set_mode(pid_ctrl_t *p, pid_mode_t mode)
{
    if ((p == NULL) || ((mode != PID_MODE_POSITIONAL) &&
                        (mode != PID_MODE_INCREMENTAL))) {
        return;
    }

    p->cfg.mode = mode;
}

void pid_track(pid_ctrl_t *p, float sp, float pv, float out)
{
    float err;

    if (p == NULL) {
        return;
    }
    if (!finite_(sp) || !finite_(pv) || !finite_(out)) {
        return;
    }

    err = sp - pv;
    p->integ = 0.0f;

    if (finite_(p->cfg.ki) && (absf_(p->cfg.ki) > 1e-9f)) {
        /* 反算积分：切换瞬间位置式输出正好等于 out（微分项置零） */
        p->integ = (out - (p->cfg.kp * err)) / p->cfg.ki;
    }

    p->prev_err = err;
    p->prev_pv = pv;
    p->d_state = 0.0f;
    p->d_state_prev = 0.0f;
    p->out = clampf_(out, p->cfg.out_min, p->cfg.out_max);
    p->out_raw = p->out;
    p->inited = true;
}

/* ============================ 核心 ============================ */

float pid_update(pid_ctrl_t *p, float sp, float pv)
{
    return pid_update_dt(p, sp, pv, (p != NULL) ? p->cfg.dt : 0.01f);
}

float pid_update_dt(pid_ctrl_t *p, float sp, float pv, float dt)
{
    float err;
    float rate;
    float d_now;
    float integ_step;
    float out_raw;
    bool  integrate = true;

    if (p == NULL) {
        return 0.0f;
    }

    /* 非法测量/设定：保持上次输出，控制器内部状态不动 */
    if (!finite_(sp) || !finite_(pv)) {
        return p->out;
    }

    if (!finite_(dt) || (dt <= 0.0f)) {
        dt = p->cfg.dt;
    }
    if (!finite_(dt) || (dt <= 0.0f)) {
        return p->out;
    }

    err = sp - pv;

    if (!p->inited) {
        p->prev_err = err;
        p->prev_pv = pv;
        p->d_state = 0.0f;
        p->d_state_prev = 0.0f;
        p->inited = true;
    }

    /* ---- 微分：先算速率，再过一阶低通 ---- */
    if (p->cfg.d_on_pv) {
        rate = -(pv - p->prev_pv) / dt;   /* d(sp-pv)/dt，sp 突变不计入 */
    } else {
        rate = (err - p->prev_err) / dt;
    }

    if (finite_(p->cfg.d_lpf_tau) && (p->cfg.d_lpf_tau > 0.0f)) {
        float alpha = dt / (p->cfg.d_lpf_tau + dt);
        p->d_state += alpha * (rate - p->d_state);
        d_now = p->d_state;
    } else {
        p->d_state = rate;
        d_now = rate;
    }

    /* ---- 积分分离 ---- */
    if (finite_(p->cfg.i_sep_err) && (p->cfg.i_sep_err > 0.0f) &&
        (absf_(err) > p->cfg.i_sep_err)) {
        integrate = false;
    }

    integ_step = integrate ? (err * dt) : 0.0f;

    if (p->cfg.mode == PID_MODE_INCREMENTAL) {
        /* 增量式：输出累加器自带抗饱和（限幅后不保留溢出量） */
        float du = (p->cfg.kp * (err - p->prev_err))
                 + (p->cfg.ki * integ_step)
                 + (p->cfg.kd * (d_now - p->d_state_prev));
        out_raw = p->out + du;
    } else {
        float integ_new = p->integ + integ_step;

        out_raw = (p->cfg.kp * err) + (p->cfg.ki * integ_new)
                + (p->cfg.kd * d_now);

        if (p->cfg.i_clamp) {
            /* 输出已顶到限幅，且本拍积分还在往同方向推：冻结积分 */
            if (((out_raw > p->cfg.out_max) && (integ_step > 0.0f)) ||
                ((out_raw < p->cfg.out_min) && (integ_step < 0.0f))) {
                integ_new = p->integ;
                out_raw = (p->cfg.kp * err) + (p->cfg.ki * integ_new)
                        + (p->cfg.kd * d_now);
            }
        }

        p->integ = integ_new;
    }

    p->out_raw = out_raw;
    p->out = clampf_(out_raw, p->cfg.out_min, p->cfg.out_max);

    p->prev_err = err;
    p->prev_pv = pv;
    p->d_state_prev = d_now;

    return p->out;
}
