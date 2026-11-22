"use strict";

const mvTreatments = [
  ["narrative", "剧情叙事"], ["performance", "舞台表演"], ["hybrid", "剧情＋表演"], ["concept", "意象与节奏"],
];
const mvStyles = [
  ["cinematic", "电影写实"], ["retro", "复古胶片"], ["stage", "霓虹舞台"], ["monochrome", "黑白高反差"],
  ["dream", "中式梦核"], ["animation", "二维动画"], ["stop-motion", "手工定格"], ["minimal", "极简意象"],
];
const mvRoles = {narrative: "剧情", performance: "表演", insert: "空镜 / 插镜"};
const mvTracks = new Map();
const mvLoads = new Set();

function mtvTime(ms) {
  const time = Math.max(0, Math.round(ms));
  return String(Math.floor(time / 60000)).padStart(2, "0") + ":" +
    String(Math.floor(time / 1000) % 60).padStart(2, "0") + "." + String(time % 1000).padStart(3, "0");
}

function mtvSettings(project) {
  if (!project.mtv) project.mtv = {
    track_id: "", lyrics: "", cues: "", direction: "", count: 1, cast_ids: [],
    style: "cinematic", treatment: "narrative", adopted_plan: "",
  };
  return project.mtv;
}

function loadMtvTrack(project, key) {
  if (!key || mvTracks.has(key) || mvLoads.has(key)) return;
  mvLoads.add(key);
  api(endpoint("/mtv-audio/" + key)).then(track => {
    mvTracks.set(key, track);
    if (state.project === project && state.page === "mtv") render();
  }).catch(error => {
    if (state.project === project) notice(error.message, true);
  }).finally(() => mvLoads.delete(key));
}

async function importMtvTrack(project, settings, file) {
  const url = endpoint("/mtv-audio?filename=" + encodeURIComponent(file.name));
  notice("正在导入音频并核对时长…");
  const track = await api(url, file);
  mvTracks.set(track.id, track);
  if (state.project !== project) return;
  update(settings, "track_id", track.id);
  update(settings, "cues", "");
  await saveProject();
  render();
  notice("音轨已保存。换歌后可以重新试听并标记时间点。");
}

async function importMtvLyrics(settings, file) {
  const content = await file.text();
  if (content.length > 30000) throw Error("歌词超过 30000 字，请拆分后再导入。");
  if (settings.lyrics.trim() && content !== settings.lyrics && !confirm("用这份歌词文件替换当前歌词？")) return;
  update(settings, "lyrics", content.replace(/^\ufeff/, ""));
  render();
}

function mvAudioPanel(project, settings, track) {
  const player = track ? node("audio", {id: "mtv-track-player", src: endpoint("/mtv-audio/" + track.id + "/preview"),
    controls: true, preload: "metadata", "aria-label": "试听 MTV 原音"}) : null;
  const cues = inputControl(settings, "cues", "00:14.250 副歌\n00:31.000 间奏", 3,
    "每行一个时间点。手动锚点会保留在分镜边界上。");
  return panel(node("h3", {}, "1. 歌曲与歌词"),
    field("声音文件 · WAV / MP4", uploadControl(".wav,.mp4,audio/wav,video/mp4",
      file => importMtvTrack(project, settings, file), "MP4 使用第一条音轨，保留上传的原文件。")),
    track ? note(track.name + " · " + mtvTime(track.duration * 1000)) : settings.track_id ? note("正在读取已保存音轨…") : null,
    player,
    field("导入歌词 · TXT / LRC", uploadControl(".txt,.lrc,text/plain", file => importMtvLyrics(settings, file))),
    field("歌词", inputControl(settings, "lyrics", "可粘贴普通歌词或 [00:12.50] 带时标歌词", 6)),
    field("分镜锚点 · 可选", cues),
    player ? button("标记当前播放位置", () => {
      const current = player.currentTime;
      if (!Number.isFinite(current)) return;
      const next = (settings.cues.trimEnd() ? settings.cues.trimEnd() + "\n" : "") + mtvTime(current * 1000) + " ";
      update(settings, "cues", next);
      cues.value = next;
      cues.focus();
    }, {help: "试听时记下主歌、副歌、停顿等位置，可在上方补写说明。"}) : null,
    note("锚点会成为分镜边界。带时间的歌词会进入相应镜头，普通歌词用于创意参考。"));
}

function mvCastPanel(project, settings) {
  const chosen = settings.cast_ids.map(id => project.cast.find(person => person.id === id)).filter(Boolean);
  const changeCount = next => {
    update(settings, "count", Number(next));
    if (settings.cast_ids.length > Number(next)) update(settings, "cast_ids", settings.cast_ids.slice(0, Number(next)));
    render();
  };
  const people = project.cast.map(person => node("label", {class: "check"}, node("input", {
    type: "checkbox", checked: settings.cast_ids.includes(person.id),
    disabled: !settings.cast_ids.includes(person.id) && settings.cast_ids.length >= settings.count,
    onchange: event => {
      update(settings, "cast_ids", event.target.checked ? [...settings.cast_ids, person.id] : settings.cast_ids.filter(id => id !== person.id));
      render();
    },
  }), person.name));
  const editors = chosen.map(person => node("details", {class: "mtv-person"},
    node("summary", {}, person.name + (person.adopted ? " · 已采用定妆" : " · 待确认定妆")),
    castCard(person)));
  return panel(node("h3", {}, "2. 人物定妆"),
    field("出镜人数", selectControl(Array.from({length: 6}, (_, i) => [String(i), i ? i + " 人" : "0 人 · 纯意象"]), settings.count,
      changeCount, "人数上限为 5。可复用项目里已有的定妆。")),
    settings.count ? node("div", {class: "row"}, people) : null,
    settings.count ? button("新增定妆人物", () => {
      const person = newCast("MTV 人物 " + (settings.cast_ids.length + 1));
      update(project, "cast", [...project.cast, person]);
      update(settings, "cast_ids", [...settings.cast_ids, person.id]);
      render();
      $("mtv-cast").querySelector(".mtv-person:last-child").open = true;
    }, {disabled: settings.cast_ids.length >= settings.count}) : null,
    settings.count ? note("选人后可展开上传参考图、生成定妆并采用。") : null,
    node("div", {id: "mtv-cast"}, editors));
}

function mvDraftView(project, settings, job) {
  const result = job.result;
  const adopted = settings.adopted_plan === job.id;
  const trackMatches = settings.track_id === result.track_id;
  const intervals = result.shots.map(shot => node("details", {class: "mtv-shot"},
    node("summary", {}, mtvTime(shot.start_ms) + " → " + mtvTime(shot.end_ms) + " · " + shot.name + " · " + mvRoles[shot.kind]),
    node("p", {}, shot.action),
    note(shot.cue),
    shot.lyrics.length ? note(shot.lyrics.map(line => line.line).join(" / ")) : null,
    trackMatches ? button("试听起点", async () => {
      const player = $("mtv-track-player");
      if (player) {
        player.currentTime = shot.start_ms / 1000;
        await player.play();
      }
    }) : null));
  return panel(node("h3", {}, "MTV 剧本与分镜草稿"),
    note(result.track_name + " · " + result.timing + " · " + result.shots.length + " 镜"),
    node("p", {class: "mtv-treatment"}, result.script), intervals,
    button(adopted ? "已加入分镜" : "加入镜头工作台", async () => {
      if (settings.adopted_plan === job.id || settings.track_id !== result.track_id) return;
      const shots = result.shots.map(source => {
        const shot = newShot({name: source.name, prompt: source.action, image_prompt: source.image_prompt});
        shot.seconds = (source.end_ms - source.start_ms) / 1000;
        shot.refs = source.ref_ids;
        shot.mtv = {track_id: result.track_id, start_ms: source.start_ms, end_ms: source.end_ms, kind: source.kind, cue: source.cue};
        return shot;
      });
      update(project, "shots", [...project.shots, ...shots]);
      update(settings, "adopted_plan", job.id);
      await saveProject();
      state.shotId = shots[0].id;
      go("shot");
    }, {primary: true, disabled: adopted || !trackMatches,
      help: "保留原有分镜，将本次兼容的时间范围一起加入。"}));
}

function mtvPage() {
  const project = state.project;
  const settings = mtvSettings(project);
  loadMtvTrack(project, settings.track_id);
  const track = mvTracks.get(settings.track_id);
  const job = mostRecent("mtv-plan");
  const ready = settings.cast_ids.length === settings.count &&
    settings.cast_ids.every(id => project.cast.some(person => person.id === id && person.adopted));
  const style = panel(node("h3", {}, "3. 形式与画风"),
    node("div", {class: "grid"},
      field("MTV 形式", selectControl(mvTreatments, settings.treatment, value => update(settings, "treatment", value),
        "选择以剧情、表演或意象为主，也可交替安排剧情和表演。")),
      field("画面风格", selectControl(mvStyles, settings.style, value => update(settings, "style", value)))),
    field("导演要求 · 可选", inputControl(settings, "direction", "例如：副歌用雨中霓虹，不安排正脸演唱特写", 3)),
    row(button("生成 MTV 剧本与分镜", () => start("mtv-plan"), {primary: true, disabled: !track || !ready || !textModelReady(),
      help: "使用所选文字模型。上传的声音和参考图保留在本机。"}),
    node("span", {class: "subtle"}, textModelLabel())),
    !ready ? note("请先确认所选人数和每人的定妆图。") : null);
  return [title("MTV 音乐短片", "先定歌曲、角色和风格，再把画面安排到原音时间轴。"),
    mvAudioPanel(project, settings, track), mvCastPanel(project, settings), style,
    job ? mvDraftView(project, settings, job) : null].filter(Boolean);
}
