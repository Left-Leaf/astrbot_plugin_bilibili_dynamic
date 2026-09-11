/**
 * AstrBot B 站蹲饼插件 —— Node 桥接进程。
 *
 * 该进程由插件（Python）以子进程方式拉起，职责只有三件事：
 *   1. 驱动 `bilibili-user-simulation` 内核（打开浏览器 → 登录 → 蹲饼 / 模拟行为）；
 *   2. 把内核事件（就绪 / 状态 / 二维码 / 动态 / 日志）打成协议行写给宿主；
 *   3. 从 stdin 读取宿主下发的「控制台指令」，交给 `kernel.executeCommand()` 执行并回传结果。
 *
 * 协议（stdout，每行一个 JSON，前缀 `@@BDYN@@`；无前缀的输出视为普通日志）：
 *   宿主 ← 进程：{type:"ready"|"status"|"qrcode"|"dynamics"|"log"|"result"|"fatal"|"bye", ...}
 *   宿主 → 进程：{"id":"1","line":"fetch on"}                    —— 内核指令
 *              {"id":"2","op":"follow","uid":"161775300"}    —— 运行态关注 UP
 *              把 line 设为 `__shutdown__` 表示关闭内核并退出。
 *
 * 用法：node --import tsx bridge.ts '<json config>'
 */

import readline from 'node:readline';

import {
  SimulationKernel,
  loadPersona,
  loadPersonaFromFile,
  type BiliDynamicItem,
  type KernelInitializeOptions,
  type PersonaConfig,
} from 'bilibili-user-simulation';

import { normalizeDynamic } from './dynamics.js';

/** 协议行前缀，宿主只解析带该前缀的 stdout 行 */
const MARKER = '@@BDYN@@';

/** 二维码轮询间隔（毫秒）：登录弹窗渲染较慢，靠轮询兜住 */
const QR_POLL_INTERVAL_MS = 3000;

/** 状态心跳间隔（毫秒） */
const STATUS_INTERVAL_MS = 15000;

/** 桥接进程的启动配置（由宿主通过 argv[2] 传入） */
interface BridgeConfig {
  /** 无头模式（默认 true） */
  headless?: boolean;
  /** 内置人格 id（与 personaFile 二选一） */
  personaId?: string;
  /** 外部人格配置文件的绝对路径 */
  personaFile?: string;
  /** 人格目录（personaId 即该目录下的文件名；留空用包内默认目录） */
  personaDir?: string;
  /** 浏览器用户数据目录（持久化登录态） */
  userDataDir?: string;
  /** 指定 Chrome 可执行文件路径（未指定时用 puppeteer 缓存的浏览器） */
  chromePath?: string;
}

/** 输出一条协议事件 */
const emit = (payload: Record<string, unknown>): void => {
  process.stdout.write(`${MARKER}${JSON.stringify(payload)}\n`);
};

/** 把任意异常转换为可读文本 */
const toErrorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

/** 打印致命错误并退出（等待 stdout 冲刷后再退出） */
const fatal = (message: string): void => {
  emit({ type: 'fatal', message });
  setTimeout(() => process.exit(1), 50);
};

// 内核与库内部都用 console.* 输出日志，这里改写成协议事件（宿主统一写日志文件）
for (const level of ['log', 'info', 'warn', 'error'] as const) {
  console[level] = (...args: unknown[]): void => {
    emit({
      type: 'log',
      level: level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'info',
      message: args
        .map((arg) => (typeof arg === 'string' ? arg : JSON.stringify(arg)))
        .join(' '),
    });
  };
}

let config: BridgeConfig = {};
try {
  config = JSON.parse(process.argv[2] ?? '{}') as BridgeConfig;
} catch (error) {
  fatal(`桥接配置解析失败: ${toErrorMessage(error)}`);
}

// 指定 Chrome 路径时统一补进 launch 参数（库内部走的就是这个 puppeteer-extra 实例）
if (config.chromePath) {
  const puppeteerExtra = (await import('puppeteer-extra')).default;
  const patchedLaunch = puppeteerExtra.launch.bind(puppeteerExtra);
  (puppeteerExtra as unknown as { launch: (options?: object) => unknown }).launch = (
    options: object = {},
  ) => patchedLaunch({ executablePath: config.chromePath, ...options });
}

const kernel = SimulationKernel.getInstance();

const initOptions: KernelInitializeOptions = {
  headless: config.headless ?? true,
  waitForLogin: false,
  onDynamics: (items: BiliDynamicItem[], kind: string) => {
    // 注意：必须用箭头函数包一层，直接传 normalizeDynamic 会把 map 的下标当成 depth 参数
    emit({ type: 'dynamics', kind, items: items.map((item) => normalizeDynamic(item)) });
  },
};
if (config.userDataDir) {
  initOptions.userDataDir = config.userDataDir;
}

// 人格：优先外部文件，其次包内 id（新版库的蹲饼不再读人格里的目标，人格只影响模拟行为）
let persona: PersonaConfig | null = null;
try {
  persona = config.personaFile
    ? loadPersonaFromFile(config.personaFile)
    : loadPersona(
        config.personaId ?? 'ak-night-worker',
        config.personaDir || undefined,
      );
} catch (error) {
  fatal(`加载人格配置失败: ${toErrorMessage(error)}`);
}
if (persona) {
  initOptions.persona = persona;
}

try {
  await kernel.initialize(initOptions);
} catch (error) {
  fatal(`初始化内核失败: ${toErrorMessage(error)}`);
}
emit({
  type: 'ready',
  status: kernel.getStatus(),
});

// ===== 二维码：轮询登录弹窗容器，截图后回传 base64 PNG =====
// 库自带的终端二维码提取在内核模式下拿不到（弹窗 canvas/img 渲染更晚），因此这里直接截图。
let lastQrBase64 = '';

const pollQrCode = async (): Promise<void> => {
  const page = kernel.page;
  if (!page || kernel.getStatus().loggedIn) {
    return;
  }
  try {
    const handle = await page.$('.login-scan-box, .scan-box');
    if (!handle) {
      return;
    }
    const shot = (await handle.screenshot({ encoding: 'base64' })) as unknown;
    await handle.dispose();
    if (typeof shot === 'string' && shot && shot !== lastQrBase64) {
      lastQrBase64 = shot;
      emit({ type: 'qrcode', png: shot });
    }
  } catch {
    // 弹窗关闭 / 页面切换时忽略
  }
};

const qrTimer = setInterval(() => void pollQrCode(), QR_POLL_INTERVAL_MS);
const statusTimer = setInterval(
  () => emit({ type: 'status', status: kernel.getStatus() }),
  STATUS_INTERVAL_MS,
);

// ===== 指令通道（stdin）=====
let closing = false;

const shutdown = async (): Promise<void> => {
  if (closing) {
    return;
  }
  closing = true;
  clearInterval(qrTimer);
  clearInterval(statusTimer);
  rl.close();
  await kernel.shutdown().catch(() => undefined);
  emit({ type: 'bye' });
  setTimeout(() => process.exit(0), 50);
};

const handleLine = async (line: string): Promise<void> => {
  let command: {
    id?: string;
    line?: string;
    op?: string;
    uid?: string;
  };
  try {
    command = JSON.parse(line) as typeof command;
  } catch {
    emit({ type: 'log', level: 'warn', message: `无法解析的控制台输入: ${line}` });
    return;
  }

  const id = command.id ?? '';
  const text = (command.line ?? '').trim();
  const op = (command.op ?? '').trim().toLowerCase();

  if (text === '__shutdown__') {
    await shutdown();
    return;
  }

  try {
    // 运行态关注 UP（独立操作，不进模拟任务流；名称由库返回，拿不到就用 uid 展示）
    if (op === 'follow') {
      const uid = String(command.uid ?? '').trim();
      const result = await kernel.followUp(uid);
      const ok = result.status !== 'failed';
      const label = result.name || result.uid || uid;
      const message =
        result.status === 'now-followed'
          ? `➕ 已关注 ${label}`
          : result.status === 'followed'
            ? `✅ ${label} 已关注（无需重复关注）`
            : `❌ 关注 ${uid} 失败：${result.detail ?? '未知原因'}`;
      emit({ type: 'result', id, ok, output: message, data: result });
      emit({ type: 'status', status: kernel.getStatus() });
      return;
    }

    if (!text) {
      return;
    }
    const result = await kernel.executeCommand(text);
    emit({ type: 'result', id, ok: result.ok, output: result.output ?? '' });
  } catch (error) {
    emit({
      type: 'result',
      id,
      ok: false,
      output: `操作异常: ${toErrorMessage(error)}`,
    });
  }
  emit({ type: 'status', status: kernel.getStatus() });
};

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => void handleLine(line.trim()));
rl.on('close', () => void shutdown());

process.on('SIGINT', () => void shutdown());
process.on('SIGTERM', () => void shutdown());
