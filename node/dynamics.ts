/**
 * 动态字段归一化。
 *
 * 新版 `bilibili-user-simulation` 把蹲饼出口改成了**B 站接口原始 item**（字段完全
 * 对齐接口，未做裁剪），这里把宿主（Python）需要的信息抽成稳定结构，隔离上游字段变化：
 *
 * - 作者 / 文本 / 时间 / 链接；
 * - **全部图片**（新版 `major.opus.pics`、旧版 `major.draw.items`）；
 * - 带封面的链接（视频 `major.archive`、直播 `major.live_rcmd`、番剧 `major.pgc`、
 *   课程 `major.courses`、专栏 `major.article`）——按需求只保留封面与链接；
 * - **内嵌转发**（`item.orig`，例如「转发动态」里嵌套的原动态）按同一结构递归归一化。
 */

import {
  dynAuthor,
  dynId,
  dynPubTimeText,
  dynPubTs,
  type BiliDynamicItem,
} from 'bilibili-user-simulation';

/** 内嵌转发的最大递归层数（防接口异常导致的自我嵌套） */
const MAX_FORWARD_DEPTH = 3;

/** 带封面的链接（视频 / 直播 / 番剧 / 课程 / 专栏） */
export interface SimpleMedia {
  /** 类型：``video`` / ``live`` / ``pgc`` / ``courses`` / ``article`` */
  kind: string;
  /** 封面图 URL */
  cover: string;
  /** 跳转链接 */
  url: string;
  /** 标题（视频标题 / 直播间标题 / 专栏标题等） */
  title: string;
}

/** 一条归一化后的动态：宿主只依赖这几个稳定字段 */
export interface SimpleDynamic {
  /** 动态 id（接口 `id_str`） */
  dynId: string;
  /** 作者名 */
  author: string;
  /** 作者 uid（接口 `module_author.mid`） */
  uid: string;
  /** 作者头像 URL（接口 `module_author.face`；取不到为空串） */
  avatar: string;
  /** 动态正文（接口 `desc.text` / `opus.summary.text`） */
  text: string;
  /** 发布时间（秒时间戳，缺失为 0） */
  pubTs: number;
  /** 发布时间的相对文本（如「18分钟前」「09-10」） */
  pubTimeText: string;
  /** 动态类型（DYNAMIC_TYPE_*，如 DRAW / AV / FORWARD / LIVE_RCMD） */
  type: string;
  /** 详情页链接（opus 页 / 动态页） */
  link: string;
  /** 动态包含的全部图片 URL（新版图文、旧版图集） */
  images: string[];
  /** 视频/直播这类「封面 + 链接」内容；没有则为 null */
  video: SimpleMedia | null;
  /** 内嵌转发的原动态（已按同一结构归一化）；没有则为 null */
  forward: SimpleDynamic | null;
}

type AnyRecord = Record<string, any>;

/** 只保留对象结构，避免对 null / 字符串做属性访问 */
const asRecord = (value: unknown): AnyRecord =>
  value && typeof value === 'object' ? (value as AnyRecord) : {};

/**
 * 补全并规范化 URL。
 *
 * Args:
 *   value: 原始 URL（可能是 `//` 协议相对地址或 `http://`）。
 *
 * Returns:
 *   可直发的 https URL；无效输入返回空串。
 */
const absoluteUrl = (value: unknown): string => {
  const url = typeof value === 'string' ? value.trim() : '';
  if (!url) {
    return '';
  }
  if (url.startsWith('//')) {
    return `https:${url}`;
  }
  // B 站 CDN 支持 https，且协议端拉 http 图更容易失败
  return url.replace(/^http:\/\//, 'https://');
};

/**
 * 从 major 里收集全部图片。
 *
 * Args:
 *   major: `modules.module_dynamic.major`。
 *
 * Returns:
 *   去重后的图片 URL 列表。
 */
const collectImages = (major: AnyRecord): string[] => {
  const urls: string[] = [];
  const push = (value: unknown): void => {
    const url = absoluteUrl(value);
    if (url && !urls.includes(url)) {
      urls.push(url);
    }
  };
  const opus = asRecord(major.opus);
  if (Array.isArray(opus.pics)) {
    for (const pic of opus.pics) {
      push(asRecord(pic).url);
    }
  }
  const draw = asRecord(major.draw);
  if (Array.isArray(draw.items)) {
    for (const item of draw.items) {
      push(asRecord(item).src);
    }
  }
  return urls;
};

/**
 * 提取「带封面的链接」内容（视频 / 直播 / 番剧 / 课程 / 专栏）。
 *
 * Args:
 *   major: `modules.module_dynamic.major`。
 *
 * Returns:
 *   归一化后的媒体信息；不属于这些类型时返回 null。
 */
const collectMedia = (major: AnyRecord): SimpleMedia | null => {
  const archive = asRecord(major.archive);
  if (archive.bvid || archive.jump_url) {
    return {
      kind: 'video',
      cover: absoluteUrl(archive.cover),
      url:
        absoluteUrl(archive.jump_url) ||
        (archive.bvid ? `https://www.bilibili.com/video/${archive.bvid}` : ''),
      title: String(archive.title ?? ''),
    };
  }

  const pgc = asRecord(major.pgc);
  if (pgc.jump_url || pgc.cover) {
    return {
      kind: 'pgc',
      cover: absoluteUrl(pgc.cover),
      url: absoluteUrl(pgc.jump_url),
      title: String(pgc.title ?? ''),
    };
  }

  const courses = asRecord(major.courses);
  if (courses.jump_url || courses.cover) {
    return {
      kind: 'courses',
      cover: absoluteUrl(courses.cover),
      url: absoluteUrl(courses.jump_url),
      title: String(courses.title ?? ''),
    };
  }

  const liveRcmd = asRecord(major.live_rcmd);
  if (liveRcmd.content) {
    try {
      const info = asRecord(JSON.parse(String(liveRcmd.content)).live_play_info);
      return {
        kind: 'live',
        cover: absoluteUrl(info.cover),
        url: absoluteUrl(info.link),
        title: String(info.title ?? ''),
      };
    } catch {
      // content 不是合法 JSON —— 忽略，继续尝试其它类型
    }
  }

  const article = asRecord(major.article);
  if (article.jump_url) {
    const covers = Array.isArray(article.covers) ? article.covers : [];
    return {
      kind: 'article',
      cover: absoluteUrl(covers.length > 0 ? asRecord(covers[0]).url : ''),
      url: absoluteUrl(article.jump_url),
      title: String(article.title ?? ''),
    };
  }

  return null;
};

/**
 * 把 B 站原始动态对象归一化成宿主使用的稳定结构。
 *
 * Args:
 *   item: B 站接口原始动态对象。
 *   depth: 当前递归层数（内嵌转发用）。
 *
 * Returns:
 *   归一化后的动态；字段缺失时给安全默认值。
 */
export const normalizeDynamic = (
  item: BiliDynamicItem,
  depth = 0,
): SimpleDynamic => {
  const author = dynAuthor(item);
  const raw = item as unknown as AnyRecord;
  const modules = asRecord(raw.modules);
  const moduleDynamic = asRecord(modules.module_dynamic);
  const major = asRecord(moduleDynamic.major);
  const dynIdValue = dynId(item);
  const media = collectMedia(major);

  // 正文：`desc.text` → `opus.summary.text`（新版图文 desc 常为 null）。
  // 不用库的 `dynText()`：它默认截断到 200 字（仅供日志用），这里要完整正文。
  let text = '';
  for (const candidate of [
    asRecord(moduleDynamic.desc).text,
    asRecord(asRecord(major.opus).summary).text,
  ]) {
    if (typeof candidate === 'string' && candidate.trim()) {
      text = candidate.trim();
      break;
    }
  }
  if (!text) {
    // 视频/直播等没有正文时，用标题兜底
    text = media?.title ?? '';
  }

  const nested = raw.orig as BiliDynamicItem | undefined;

  return {
    dynId: dynIdValue,
    author: author.name,
    uid: author.uid,
    avatar: absoluteUrl(asRecord(modules.module_author).face),
    text,
    pubTs: dynPubTs(item),
    pubTimeText: dynPubTimeText(item),
    type: typeof raw.type === 'string' ? raw.type : '',
    link:
      absoluteUrl(asRecord(major.opus).jump_url) ||
      absoluteUrl(asRecord(major.article).jump_url) ||
      (dynIdValue ? `https://t.bilibili.com/${dynIdValue}` : ''),
    images: collectImages(major),
    video: media,
    forward:
      nested && depth < MAX_FORWARD_DEPTH
        ? normalizeDynamic(nested, depth + 1)
        : null,
  };
};
