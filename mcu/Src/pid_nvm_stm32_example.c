/**
 * @file    pid_nvm_stm32_example.c
 * @brief   PID 参数掉电保存示例（STM32 内部 Flash，F1/F4/H7 三族分支）
 *
 * 存放内容：pid_nvm_t（28 字节）+ 魔数 + CRC16，读回时三重校验，
 * 任何一项不符就当"空 Flash"，用默认参数启动，绝不带着坏参数跑闭环。
 *
 * 地址选择：放在最后一页/最后一个扇区，远离程序区。
 *   F103C8 (64KB)  : 0x0801FC00，页大小 1KB
 *   F407VG (1MB)   : 0x080E0000，Sector 11（128KB）
 *   H743   (2MB)   : 0x081E0000，Sector 7 Bank2（按你的链接脚本确认）
 *
 * 下面的默认值按"擦除单位"自动选族，换芯片多数情况不用动；
 * 需要写别的位置时用 -D 覆盖这三个宏，并在链接脚本里把该区域排除：
 *   PID_NVM_ADDR / PID_NVM_SECTOR / PID_NVM_BANK
 * 注意判族用擦除宏（FLASH_TYPEERASE_PAGES/SECTORS），不能用编程单位宏——
 * F4 也定义了 FLASH_TYPEPROGRAM_HALFWORD，用它判族会把 F4 误判成 F1。
 *
 * 写入策略：应用层已做 5s 静默防抖（PID_NVM_SAVE_DELAY_MS），
 * 这里只管一次擦写，不做重试——失败会上报 ERROR(0x04)。
 */

/* ---- HAL 头文件：真实工程用 CubeMX 的 main.h，仓库回归用 mcu/stubs 下的桩 ----
 * 三态宏的约定与 main_example.c 完全一致，两个示例必须同进同退。 */
#if !defined(PID_HAL_STUB)
#  if defined(__has_include)
#    if __has_include("main.h")
#      include "main.h"
#    else
#      include "stm32_hal_stub.h"
#    endif
#  else
#    include "main.h"
#  endif
#elif PID_HAL_STUB
#  include "stm32_hal_stub.h"
#else
#  include "main.h"
#endif

#include "pid_app.h"
#include "pid_comm.h"

#include <stddef.h>
#include <string.h>

/* ============================ 芯片相关宏 ============================ */
/*
 * 默认值按"Flash 擦除/编程单位"自动选族，和下面的分支判断用同一组判据：
 *   看到 FLASH_TYPEPROGRAM_FLASHWORD  -> H7      （0x081E0000，Sector 7 Bank2）
 *   看到 FLASH_TYPEERASE_PAGES        -> F1/F0/L0（0x0801FC00，页 1KB）
 *   看到 FLASH_TYPEERASE_SECTORS      -> F4/F3   （0x080E0000，Sector 11 128KB）
 * 三个地址都落在"最后一页/扇区"，改芯片时用 -D 覆盖下面三个宏即可。
 */

#if defined(FLASH_TYPEPROGRAM_FLASHWORD)
#define PID_NVM_DEFAULT_ADDR   0x081E0000u
#define PID_NVM_DEFAULT_SECTOR FLASH_SECTOR_7
#define PID_NVM_DEFAULT_BANK   FLASH_BANK_2
#elif defined(FLASH_TYPEERASE_PAGES)
#define PID_NVM_DEFAULT_ADDR   0x0801FC00u
#define PID_NVM_DEFAULT_SECTOR 0u        /* F1 按页擦除，不用扇区号 */
#define PID_NVM_DEFAULT_BANK   0u
#else
#define PID_NVM_DEFAULT_ADDR   0x080E0000u
#define PID_NVM_DEFAULT_SECTOR FLASH_SECTOR_11
#define PID_NVM_DEFAULT_BANK   FLASH_BANK_1
#endif

#ifndef PID_NVM_ADDR
#define PID_NVM_ADDR PID_NVM_DEFAULT_ADDR
#endif

#ifndef PID_NVM_SECTOR
#define PID_NVM_SECTOR PID_NVM_DEFAULT_SECTOR
#endif

#ifndef PID_NVM_BANK
#define PID_NVM_BANK PID_NVM_DEFAULT_BANK
#endif

#if defined(__GNUC__)
#define PID_NVM_ALIGN32 __attribute__((aligned(32)))
#elif defined(__CC_ARM)
#define PID_NVM_ALIGN32 __align(32)
#else
#define PID_NVM_ALIGN32
#endif

/* ============================ 公共部分 ============================ */

static uint16_t nvm_crc(const pid_nvm_t *n)
{
    return pid_crc16_modbus((const uint8_t *)n,
                            (uint16_t)offsetof(pid_nvm_t, crc));
}

/**
 * @brief  读参数：地址合法 + 魔数正确 + CRC 正确才返回 true
 * @note   直接内存读取，不需要解锁 Flash
 */
bool pid_nvm_load_impl(void *ctx, pid_nvm_t *out)
{
    const pid_nvm_t *flash = (const pid_nvm_t *)PID_NVM_ADDR;
    pid_nvm_t tmp;

    (void)ctx;

    if (out == NULL) {
        return false;
    }

    tmp = *flash;   /* 结构体 28 字节，天然按 4 字节对齐 */
    if (tmp.magic != PID_NVM_MAGIC) {
        return false;
    }
    if (tmp.crc != nvm_crc(&tmp)) {
        return false;
    }

    *out = tmp;
    return true;
}

/* ============================ F1/F0/L0：页擦除 + 半字编程 ============================ */

#if defined(FLASH_TYPEERASE_PAGES)

bool pid_nvm_save_impl(void *ctx, const pid_nvm_t *in)
{
    FLASH_EraseInitTypeDef erase;
    uint32_t               sector_err = 0u;
    const uint8_t         *src;
    uint32_t               n;
    bool                   ok = true;

    (void)ctx;

    if (in == NULL) {
        return false;
    }

    src = (const uint8_t *)in;
    n = (uint32_t)sizeof(*in);

    HAL_FLASH_Unlock();
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_EOP | FLASH_FLAG_PGERR |
                           FLASH_FLAG_WRPERR);

    memset(&erase, 0, sizeof(erase));
    erase.TypeErase   = FLASH_TYPEERASE_PAGES;
    erase.PageAddress = PID_NVM_ADDR;
    erase.NbPages     = 1u;

    if (HAL_FLASHEx_Erase(&erase, &sector_err) != HAL_OK) {
        HAL_FLASH_Lock();
        return false;
    }

    for (uint32_t i = 0u; i < n; i += 2u) {
        uint16_t hw = (uint16_t)src[i];

        if ((i + 1u) < n) {
            hw |= (uint16_t)((uint16_t)src[i + 1u] << 8);
        }

        if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD,
                              PID_NVM_ADDR + i, (uint64_t)hw) != HAL_OK) {
            ok = false;
            break;
        }
    }

    HAL_FLASH_Lock();

    /* 回读比对：写坏的 Flash 不能假装成功 */
    if (ok) {
        ok = (memcmp((const void *)PID_NVM_ADDR, in, (size_t)n) == 0);
    }

    return ok;
}

/* ============================ F4/F3：扇区擦除 + 字编程 ============================ */

#elif defined(FLASH_TYPEERASE_SECTORS) && defined(FLASH_TYPEPROGRAM_WORD)

bool pid_nvm_save_impl(void *ctx, const pid_nvm_t *in)
{
    FLASH_EraseInitTypeDef erase;
    uint32_t               sector_err = 0u;
    const uint8_t         *src;
    uint32_t               n;
    bool                   ok = true;

    (void)ctx;

    if (in == NULL) {
        return false;
    }

    src = (const uint8_t *)in;
    n = (uint32_t)sizeof(*in);

    HAL_FLASH_Unlock();
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_EOP | FLASH_FLAG_OPERR |
                           FLASH_FLAG_WRPERR | FLASH_FLAG_PGAERR |
                           FLASH_FLAG_PGPERR | FLASH_FLAG_PGSERR);

    memset(&erase, 0, sizeof(erase));
    erase.TypeErase    = FLASH_TYPEERASE_SECTORS;
    erase.Sector       = PID_NVM_SECTOR;
    erase.NbSectors    = 1u;
    erase.VoltageRange = FLASH_VOLTAGE_RANGE_3;   /* 2.7~3.6V，字编程 */

    if (HAL_FLASHEx_Erase(&erase, &sector_err) != HAL_OK) {
        HAL_FLASH_Lock();
        return false;
    }

    for (uint32_t i = 0u; i < n; i += 4u) {
        uint32_t word = 0u;

        for (uint32_t b = 0u; (b < 4u) && ((i + b) < n); b++) {
            word |= (uint32_t)src[i + b] << (8u * b);
        }

        if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_WORD,
                              PID_NVM_ADDR + i, (uint64_t)word) != HAL_OK) {
            ok = false;
            break;
        }
    }

    HAL_FLASH_Lock();

    if (ok) {
        ok = (memcmp((const void *)PID_NVM_ADDR, in, (size_t)n) == 0);
    }

    return ok;
}

/* ============================ H7：扇区擦除 + 256bit flashword ============================ */
/*
 * H7 的编程单位是 32 字节 flashword，地址和目标缓冲都必须 32 字节对齐。
 * 本分支只做了 API 层面的正确实现，未在实机上验证：
 * 上板前请确认 PID_NVM_ADDR 落在你链接脚本留空的扇区，且地址 32 字节对齐。
 */
#elif defined(FLASH_TYPEPROGRAM_FLASHWORD)

bool pid_nvm_save_impl(void *ctx, const pid_nvm_t *in)
{
    FLASH_EraseInitTypeDef                 erase;
    uint32_t                               sector_err = 0u;
    static uint32_t PID_NVM_ALIGN32        word_buf[8];   /* 32 字节 */
    bool                                   ok = true;

    (void)ctx;

    if (in == NULL) {
        return false;
    }
    if ((sizeof(*in) > sizeof(word_buf)) || ((PID_NVM_ADDR & 0x1Fu) != 0u)) {
        return false;   /* 结构变大或地址未对齐，直接拒绝，不写坏 Flash */
    }

    memset(word_buf, 0, sizeof(word_buf));
    memcpy(word_buf, in, sizeof(*in));

    HAL_FLASH_Unlock();
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);

    memset(&erase, 0, sizeof(erase));
    erase.TypeErase = FLASH_TYPEERASE_SECTORS;
    erase.Banks     = PID_NVM_BANK;
    erase.Sector    = PID_NVM_SECTOR;
    erase.NbSectors = 1u;

    if (HAL_FLASHEx_Erase(&erase, &sector_err) != HAL_OK) {
        HAL_FLASH_Lock();
        return false;
    }

    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_FLASHWORD, PID_NVM_ADDR,
                          (uint64_t)(uintptr_t)word_buf) != HAL_OK) {
        ok = false;
    }

    HAL_FLASH_Lock();

    if (ok) {
        ok = (memcmp((const void *)PID_NVM_ADDR, in, sizeof(*in)) == 0);
    }

    return ok;
}

#else
#error "未识别的 Flash 编程单位：请在 pid_nvm_stm32_example.c 里补一个分支"
#endif

/* ============================ 挂到应用层 ============================ */

/*
 * 在 PID_Example_Init 里把这两个函数挂上即可（示例代码里默认是 NULL）：
 *
 *   io.nvm_load = pid_nvm_load_impl;
 *   io.nvm_save = pid_nvm_save_impl;
 *
 * 想用 0x71 做真复位：
 *   static void bsp_system_reset(void *ctx) { (void)ctx; NVIC_SystemReset(); }
 *   io.system_reset = bsp_system_reset;
 */
