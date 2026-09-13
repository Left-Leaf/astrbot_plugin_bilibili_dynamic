/**
 * AstrBot B 站蹲饼插件 —— Node 桥接进程。
 *
 * 该进程由插件（Python）以子进程方式拉起，职责只有三件事：
 *   1. 驱动 `bilibili-user-simulation` 内核（打开浏览器 → 登录 → 蹲饼 / 模拟行为）；
 *   2. 把内核事件（就绪 / 状态 / 二维码 / 动态 / 日志）打成协议行写给宿主；
 *   3. 从 stdin 读取宿主下发的「控制台指令」，映射到内核的六类公开能力并回传结果。
 *
 * 新版内核（`refactor!: 内核收口为六类能力`）**不再提供** `executeCommand()` / `getStatus()` /
 * `initialize({onDynamics})`，所以桥接层自己承担两件事：
 *   - 把控制台指令映射到 initialize/destroy、startFetch/stopFetch、startSimulation/stopSimulation、
 *     createDynamicListener、login/logout、followUp；
 *   - 自己维护状态（登录态 / 蹲饼与模拟开关 / 已捕获条数 / 增量基线），供控制台展示。
 *
 * 协议（stdout，每行一个 JSON，前缀 `@@BDYN@@`；无前缀的输出视为普通日志）：
 *   宿主 ← 进程：{type:"ready"|"status"|"qrcode"|"dynamics"|"log"|"result"|"fatal"|"bye", ...}
 *   宿主 → 进程：{"id":"1","line":"fetch on"}                    —— 控制台指令
 *              {"id":"2","op":"follow","uid":"161775300"}    —— 运行态关注 UP
 *              把 line 设为 `__shutdown__` 表示关闭内核并退出。
 *
 * 用法：node --import tsx bridge.ts '<json config>'
 */

import readline from 'node:readline';

import {
  dynPubTs,
  kernel,
  type BiliDynamicItem,
  type DynamicSubscription,
  type KernelInitializeOptions,
  type LoginQrPayload,
} from 'bilibili-user-simulation';

import { normalizeDynamic, type SimpleDynamic } from './dynamics.js';

/** 协议行前缀，宿主只解析带该前缀的 stdout 行 */
const MARKER = '@@BDYN@@';

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
  /** 增量基线（秒）：上次已见动态的最新发布时间，内核只投递它之后的动态（不缺失） */
  baselineTs?: number;
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

/**
 * 内核登录二维码回调：登录阶段每发布一张二维码回调一次，换码时 `fingerprint` 会变。
 *
 * 二维码只有 `login({ onQrcode })` 这一个出口（`initialize()` 不接受该回调）。
 */
let lastQrFingerprint = '';
const onQrcode = (qr: LoginQrPayload): void => {
  if (qr.fingerprint && qr.fingerprint === lastQrFingerprint) {
    return;
  }
  lastQrFingerprint = qr.fingerprint;
  // 内核给的是 `data:image/png;base64,...`，协议里传裸 base64（页面自己拼 data URL）
  const png = (qr.imageBase64 ?? '').replace(/^data:image\/\w+;base64,/, '');
  if (png) {
    emit({ type: 'qrcode', png, url: qr.url ?? '' });
  } else {
    // 只解出链接、没拿到图片时，至少把链接写进日志（控制台可见）
    emit({
      type: 'log',
      level: 'warn',
      message: qr.url
        ? `登录二维码未取到图片，请用链接登录：${qr.url}`
        : '登录二维码未取到（请查看浏览器窗口）',
    });
  }
};

// 指定 Chrome 路径时统一补进 launch 参数（库内部走的就是这个 puppeteer-extra 实例）
if (config.chromePath) {
  const puppeteerExtra = (await import('puppeteer-extra')).default;
  const patchedLaunch = puppeteerExtra.launch.bind(puppeteerExtra);
  (puppeteerExtra as unknown as { launch: (options?: object) => unknown }).launch = (
    options: object = {},
  ) => patchedLaunch({ executablePath: config.chromePath, ...options });
}

/** 内核状态快照（新版内核不再提供 getStatus()，桥接层自己维护；字段与控制台页面一致） */
const state = {
  initialized: false,
  loggedIn: false,
  fetchRunning: false,
  simulationRunning: false,
  dynamicCount: 0,
};

/** 增量基线（秒）：随已投递动态与 `stopFetch()` 返回值推进，下次 startFetch 传回 */
let baselineTs = Number(config.baselineTs) || 0;

/** 最近投递的动态（新的在后；只留一小段给控制台 `dynamics [n]`） */
const recentDynamics: SimpleDynamic[] = [];
const RECENT_LIMIT = 50;

/** 上报一次状态快照 */
const emitStatus = (): void => emit({ type: 'status', status: state });

const initOptions: KernelInitializeOptions = {
  headless: config.headless ?? true,
  // 登录交给下面的登录探针 / 控制台 login 指令驱动，初始化本身不阻塞等待扫码
  // （初始化不接受 onQrcode：二维码只从 login({ onQrcode }) 出来）
  waitForLogin: false,
};
if (config.userDataDir) {
  initOptions.userDataDir = config.userDataDir;
}
if (config.personaDir) {
  initOptions.personaDir = config.personaDir;
}
if (config.personaFile) {
  initOptions.personaFile = config.personaFile;
} else {
  initOptions.personaId = config.personaId ?? 'ak-night-worker';
}

try {
  await kernel.initialize(initOptions);
} catch (error) {
  fatal(`初始化内核失败: ${toErrorMessage(error)}`);
}
state.initialized = true;
emit({ type: 'ready', status: state });

// ===== 登录：内核只暴露 login()/logout()，登录态取 login() 的返回值 =====
let loginTask: Promise<boolean> | null = null;

/** 登录（幂等：并发调用共用同一个流程）；已登录时内核立即返回 true，未登录则等待扫码 */
const loginOnce = (): Promise<boolean> => {
  if (!loginTask) {
    loginTask = kernel
      .login({ onQrcode })
      .then((ok) => {
        state.loggedIn = ok;
        return ok;
      })
      .finally(() => {
        loginTask = null;
      });
  }
  return loginTask;
};

// 初始化后自动探针：已登录立即置位；未登录则会进入扫码等待（控制台会显示二维码）
void loginOnce().then(
  () => emitStatus(),
  (error) =>
    emit({
      type: 'log',
      level: 'warn',
      message: `登录流程异常: ${toErrorMessage(error)}`,
    }),
);

// ===== 动态投递：新版内核用「监听器」（不再有 initialize 的 onDynamics 回调）=====
const subscription: DynamicSubscription = kernel.createDynamicListener((items, kind) => {
  // 新版两种 kind 都是「增量」（不是全量快照），直接追加即可
  const normalized = items.map((item: BiliDynamicItem) => {
    const newest = dynPubTs(item);
    if (newest > baselineTs) {
      baselineTs = newest;
    }
    return normalizeDynamic(item);
  });
  state.dynamicCount += normalized.length;
  recentDynamics.push(...normalized);
  if (recentDynamics.length > RECENT_LIMIT) {
    recentDynamics.splice(0, recentDynamics.length - RECENT_LIMIT);
  }
  emit({ type: 'dynamics', kind, items: normalized });
});

const statusTimer = setInterval(emitStatus, STATUS_INTERVAL_MS);

// ===== 指令通道（stdin）=====
let closing = false;

const shutdown = async (): Promise<void> => {
  if (closing) {
    return;
  }
  closing = true;
  clearInterval(statusTimer);
  rl.close();
  subscription.cancel();
  await kernel.destroy().catch(() => undefined);
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

  const reply = (ok: boolean, output: string, data?: unknown): void => {
    emit({ type: 'result', id, ok, output, ...(data ? { data } : {}) });
    emitStatus();
  };

  /** 秒时间戳 → 本地可读时间 */
  const readTime = (ts: number): string =>
    ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '（未设置）';

  try {
    const [head, ...rest] = text.split(/\s+/);
    const action = (rest[0] ?? '').toLowerCase();

    // 运行态关注 UP（独立操作，不进模拟任务流）：聊天侧发 op=follow，控制台可输入 `follow <uid>`
    if (op === 'follow' || head === 'follow') {
      const uid =
        op === 'follow' ? String(command.uid ?? '').trim() : (rest[0] ?? '').trim();
      if (!uid) {
        reply(false, '用法：follow <uid 或空间链接>');
        return;
      }
      const result = await kernel.followUp(uid);
      const ok = result.status !== 'failed';
      const label = result.name || result.uid || uid;
      const message =
        result.status === 'now-followed'
          ? `➕ 已关注 ${label}`
          : result.status === 'followed'
            ? `✅ ${label} 已关注（无需重复关注）`
            : `❌ 关注 ${uid} 失败：${result.detail ?? '未知原因'}`;
      reply(ok, message, result);
      return;
    }

    if (!text) {
      return;
    }

    if (head === 'login') {
      if (state.loggedIn) {
        reply(true, '✅ 已是登录状态。');
        return;
      }
      if (loginTask) {
        reply(true, '⏳ 登录流程进行中：请用 B 站 App 扫上方二维码，完成后状态会自动刷新。');
        return;
      }
      void loginOnce().then(
        (ok) => reply(ok, ok ? '✅ 登录成功。' : '❌ 登录未完成，请重试。'),
        (error) => reply(false, `登录异常: ${toErrorMessage(error)}`),
      );
      reply(true, '⏳ 已开始登录：二维码稍后显示在上方，请用 B 站 App 扫码。');
      return;
    }

    if (head === 'fetch') {
      if (action === 'on') {
        // 先快照本次请求的基线：startFetch 期间首批动态会投递并把 baselineTs 推进，
        // 回执必须说「本次真正传给内核的值」，否则会和内核日志里的基线对不上
        const requested = baselineTs;
        const covered = await kernel.startFetch({
          baselineTs: requested || undefined,
        });
        state.fetchRunning = true;
        const range = requested
          ? `基线 ${readTime(requested)} 之后`
          : '当前时间之后';
        reply(
          true,
          covered
            ? `✅ 蹲饼已开启（${range}的动态都会投递）`
            : `✅ 蹲饼已开启：首屏还没覆盖到基线，内核会继续滚动补全`,
        );
        return;
      }
      if (action === 'off') {
        const finalBaseline = await kernel.stopFetch();
        state.fetchRunning = false;
        if (finalBaseline > baselineTs) {
          baselineTs = finalBaseline;
        }
        reply(true, `🛑 蹲饼已关闭（本次最终基线 ${readTime(baselineTs)}）`);
        return;
      }
      reply(false, '用法：fetch on / fetch off');
      return;
    }

    if (head === 'sim') {
      if (action === 'on') {
        await kernel.startSimulation();
        state.simulationRunning = true;
        reply(true, '✅ 模拟人格已开启（养号任务流）。');
        return;
      }
      if (action === 'off') {
        await kernel.stopSimulation();
        state.simulationRunning = false;
        reply(true, '🛑 模拟人格已结束。');
        return;
      }
      reply(false, '用法：sim on / sim off');
      return;
    }

    if (head === 'status') {
      reply(
        true,
        [
          `初始化：${state.initialized ? '已完成' : '未初始化'}`,
          `登录态：${state.loggedIn ? '已登录' : '未登录'}`,
          `蹲饼：${state.fetchRunning ? '运行中' : '已关闭'}`,
          `模拟人格：${state.simulationRunning ? '运行中' : '已结束'}`,
          `已捕获动态：${state.dynamicCount} 条`,
          `增量基线：${readTime(baselineTs)}`,
          `人格：${config.personaFile || config.personaId || 'ak-night-worker'}`,
        ].join('\n'),
      );
      return;
    }

    if (head === 'dynamics') {
      const limit = Math.max(Number(rest[0]) || 5, 1);
      const picked = recentDynamics.slice(-limit).reverse();
      reply(
        true,
        picked.length
          ? picked
              .map((item: SimpleDynamic) => {
                // 按码点截断：直接 slice 会把 emoji 劈成半个代理字符，宿主无法编码
                const text = Array.from(item.text || '（无正文）').slice(0, 60).join('');
                return `· ${item.author || item.uid || '未知 UP 主'}${
                  item.pubTimeText ? ` · ${item.pubTimeText}` : ''
                }：${text}`;
              })
              .join('\n')
          : '（本次运行还没有捕获到动态）',
      );
      return;
    }

    reply(false, `未知指令：${text}`);
  } catch (error) {
    emit({
      type: 'result',
      id,
      ok: false,
      output: `操作异常: ${toErrorMessage(error)}`,
    });
    emitStatus();
  }
};

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => void handleLine(line.trim()));
rl.on('close', () => void shutdown());

process.on('SIGINT', () => void shutdown());
process.on('SIGTERM', () => void shutdown());
