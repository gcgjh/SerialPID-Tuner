/**
 * @file    pid_calc.h
 * @brief   离散 PID 算法（位置式 / 增量式，可配置，无 HAL 依赖）
 *
 * 单位约定：
 *   - dt 单位秒
 *   - ki 的量纲是 “1/(秒·误差单位)”，即 ki*Σ(err*dt)
 *   - kd 的量纲是 “秒”，即 kd*d(err)/dt
 *   - 输出 out 单位由调用方决定（例：PWM 占空比 0..100）
 *
 * 实现要点：
 *   1. 位置式与增量式共用同一套“速率 - 一阶低通 - 差分”的微分路径，
 *      因此两种模式在相同参数下响应一致（自带一致性单测）。
 *   2. 微分先行（d_on_pv）：对 PV 微分，设定值突变不会打出一个 D 冲击。
 *   3. 抗积分饱和：位置式采用条件积分冻结；增量式靠输出累加器限幅天然抗饱和。
 *   4. 积分分离：|err| > i_sep_err 时暂停积分，用于大偏差快速跟踪阶段。
 *   5. 输入 NaN/Inf 时保持上次输出，绝不把 NaN 传给执行器。
 */

#ifndef PID_CALC_H
#define PID_CALC_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    PID_MODE_POSITIONAL  = 0, /**< 位置式：out = Kp*e + Ki*∫e + Kd*de/dt */
    PID_MODE_INCREMENTAL = 1  /**< 增量式：Δu = Kp*Δe + Ki*e*dt + Kd*Δ(de/dt) */
} pid_mode_t;

/** 可调参数（掉电保存的就是这一组 + 设定值） */
typedef struct {
    float      kp;
    float      ki;
    float      kd;
    float      out_min;    /**< 输出下限，例 0 */
    float      out_max;    /**< 输出上限，例 100 */
    float      dt;         /**< 名义采样周期(s)，>0 */
    pid_mode_t mode;
    bool       d_on_pv;    /**< true=对 PV 微分（推荐）；false=对误差微分 */
    float      d_lpf_tau;  /**< 微分一阶低通时间常数(s)，<=0 关闭 */
    float      i_sep_err;  /**< 积分分离阈值，<=0 关闭 */
    bool       i_clamp;    /**< true=饱和时冻结积分（推荐） */
} pid_cfg_t;

/** 运行状态（不上 flash，上电清零） */
typedef struct {
    pid_cfg_t cfg;
    float     integ;         /**< 积分累加器，单位 err*s */
    float     prev_err;
    float     prev_pv;
    float     d_state;       /**< 微分低通状态（速率） */
    float     d_state_prev;  /**< 上一拍速率，用于增量式 */
    float     out;           /**< 上一拍已限幅输出 */
    float     out_raw;       /**< 上一拍未限幅输出（调试用） */
    bool      inited;
} pid_ctrl_t;

/** 默认配置：kp=1 ki=0 kd=0, out 0..100, dt=0.01s, 位置式, 微分先行, 无滤波 */
void pid_cfg_default(pid_cfg_t *cfg);

/** NaN / ±Inf 判定（不引 libm，供应用层校验上位机下发的 float） */
bool pid_float_is_finite(float v);

/** 初始化/复位控制器（清空积分与微分状态，输出置 out_init 限幅值） */
void pid_init(pid_ctrl_t *p, const pid_cfg_t *cfg);
void pid_reset(pid_ctrl_t *p, float out_init);

/** 在线改参（不清状态，可用于上位机调速） */
void pid_set_gains(pid_ctrl_t *p, float kp, float ki, float kd);
void pid_set_limits(pid_ctrl_t *p, float out_min, float out_max);
void pid_set_dt(pid_ctrl_t *p, float dt);
void pid_set_mode(pid_ctrl_t *p, pid_mode_t mode);

/**
 * @brief  无扰切换：让位置式的积分项反算出 out，使切换瞬间输出不跳变
 * @note   ki==0 时只对齐输出与历史状态
 */
void pid_track(pid_ctrl_t *p, float sp, float pv, float out);

/** 用 cfg.dt 作为采样周期跑一拍 */
float pid_update(pid_ctrl_t *p, float sp, float pv);

/**
 * @brief  指定本拍实际 dt（推荐：用真实经过时间，抖动大时更准）
 * @param  dt 秒；<=0 或非法时退回 cfg.dt
 * @retval 限幅后的输出
 */
float pid_update_dt(pid_ctrl_t *p, float sp, float pv, float dt);

#ifdef __cplusplus
}
#endif

#endif /* PID_CALC_H */
