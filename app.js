"use strict";

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="director-key"]').content;
const jobNames = {
  breakdown: "剧本拆分", "mtv-plan": "MTV 剧本与分镜", rewrite: "H3 提示词", "cast-image": "定妆", "first-image": "首帧",
  video: "镜头视频", export: "导出", "cast-source": "造型参考", "first-upload": "上传首帧", "last-upload": "上传尾帧",
  "face-scan": "识别人脸", "face-replace": "换脸视频", "face-export": "导出换脸视频",
};
const modeHome = {drama: "script", mtv: "mtv", swap: "swap"};
const state = {
  project: null, projects: [], jobs: [], page: "script", shotId: null, dirty: false,
  busy: false, capabilities: {}, profiles: {}, sizes: {}, exporting: "h264", splash: false,
  native: null, textModel: null, task: null, monitorOpen: false,
  mode: "drama", availableModes: ["drama"], faceReady: false, valtzError: "", projectByMode: {},
};
let saveInFlight = null;
let editCount = 0;
let pollTimer;

function node(tag, options = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(options)) {
    if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else if (name === "value") element.value = value;
    else if (name === "class") element.className = value;
    else if (value !== false && value !== null && value !== undefined) element.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) element.append(child);
  }
  return element;
}

const title = (headline, description) =>
  node("div", {class: "intro"}, node("h2", {}, headline), node("p", {class: "subtle"}, description));
const panel = (...children) => node("article", {class: "panel"}, children);
const row = (...children) => node("div", {class: "row"}, children);
const note = text => node("p", {class: "note"}, text);
const field = (label, input) => node("label", {class: "field"}, label, input);
const button = (label, action, options = {}) => node("button", {
  type: "button", class: options.primary ? "primary" : "", title: options.help,
  disabled: options.disabled, onclick: () => perform(action),
}, label);

function notice(text, error = false) {
  $("message").hidden = !text;
  $("message").textContent = text || "";
  $("message").className = error ? "error" : "";
}

async function perform(action) {
  try { await action(); }
  catch (error) { notice(error.message || String(error), true); }
}

async function api(path, value) {
  const options = value === undefined ? {} : {
    method: "POST", headers: {"X-Director-Key": token},
    body: value instanceof Blob ? value : JSON.stringify(value),
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || "本机操作失败");
  return result;
}

function endpoint(suffix = "") {
  return "/api/projects/" + state.project.id + suffix;
}

function update(object, key, value) {
  object[key] = value;
  editCount++;
  state.dirty = true;
  $("save").textContent = "保存项目";
  $("project-name").textContent = state.project.name + " · 有改动";
}

function inputControl(object, key, placeholder, lines = 1, help) {
  return node(lines > 1 ? "textarea" : "input", {
    value: object[key] || "", rows: lines > 1 ? lines : undefined,
    placeholder, title: help, oninput: event => update(object, key, event.target.value),
  });
}

function selectControl(options, current, callback, help) {
  const select = node("select", {title: help, onchange: event => callback(event.target.value)});
  for (const [id, label] of options) select.append(node("option", {value: id}, label));
  select.value = options.some(([id]) => String(id) === String(current)) ? current : options[0]?.[0] ?? "";
  return select;
}

function uploadControl(accept, callback, help) {
  return node("input", {
    type: "file", accept, title: help,
    onchange: event => {
      const file = event.target.files[0];
      if (file) perform(() => callback(file));
    },
  });
}

async function reloadCapabilities() {
  const result = await api("/api/capabilities");
  state.capabilities = result.valtz;
  state.sizes = result.sizes;
  state.profiles = result.profiles;
  state.native = result.native;
  state.valtzError = result.valtz_error || "";
  state.splash = result.splash;
  state.availableModes = result.modes || ["drama"];
  state.faceReady = !!result.face_swap;
  $("mode-switch").hidden = state.availableModes.length < 2;
  renderConnection();
  render();
}

async function reloadProjects() {
  state.projects = await api("/api/projects");
  refreshProjectPicker();
}

function refreshProjectPicker() {
  const options = state.projects.filter(project => (project.mode || "drama") === state.mode)
    .map(project => node("option", {value: project.id}, project.name));
  $("project").replaceChildren(node("option", {value: ""}, "选择项目"), ...options);
  $("project").value = state.project?.id || "";
}

async function openProject(id) {
  await saveProject();
  if (!id) {
    refreshProjectPicker();
    return;
  }
  const base = "/api/projects/" + id;
  const [project, jobs] = await Promise.all([api(base), api(base + "/jobs")]);
  state.project = project;
  state.mode = project.mode || "drama";
  state.projectByMode[state.mode] = project.id;
  state.jobs = jobs;
  state.shotId = null;
  state.dirty = false;
  refreshProjectPicker();
  go(modeHome[state.mode]);
  schedulePoll();
}

async function switchMode(mode) {
  if (mode === state.mode || !state.availableModes.includes(mode)) return;
  await saveProject();
  state.mode = mode;
  state.project = null;
  state.jobs = [];
  state.shotId = null;
  notice("");
  refreshProjectPicker();
  const last = state.projects.find(project => project.id === state.projectByMode[mode])
    || state.projects.find(project => (project.mode || "drama") === mode);
  if (last) await openProject(last.id);
  else go(modeHome[mode]);
  schedulePoll();
}

async function saveProject(showMessage = false) {
  if (!state.project) return;
  if (saveInFlight) await saveInFlight;
  if (!state.dirty) {
    if (showMessage) notice("项目已保存。");
    return;
  }
  const count = editCount;
  const project = state.project;
  saveInFlight = api(endpoint(), project).then(saved => {
    project.revision = saved.revision;
    if (editCount === count) {
      state.dirty = false;
      $("project-name").textContent = project.name;
    }
    if (showMessage) notice(state.dirty ? "还有新改动，保存后即可同步。" : "项目已保存。");
  });
  try { await saveInFlight; }
  finally { saveInFlight = null; }
}

function makeId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(12)), byte => byte.toString(16).padStart(2, "0")).join("");
}

function newCast(name = "新人物", prompt = "") {
  return {id: makeId(), name, prompt, useSources: true, adopted: ""};
}

function newShot(source = {}) {
  return {
    id: makeId(), name: source.name || "新镜头",
    seconds: Math.min(15, Math.max(2, Math.round((Number(source.seconds) || 4.5) * 2) / 2)),
    brief: source.prompt || "", imagePrompt: source.image_prompt || "", prompt: "",
    ratio: "3:4", size: "768x1024", mode: "FL2VA", quality: "balanced",
    first: "", last: "", selected: "", refs: [], refine: "auto",
  };
}

function go(page) {
  state.page = page;
  for (const control of document.querySelectorAll("[data-page]")) {
    if (control.dataset.page === page) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  }
  render();
}

function render() {
  const work = $("work");
  $("save").disabled = !state.project;
  $("project-name").textContent = !state.project ? "尚未打开项目" : state.project.name + (state.dirty ? " · 有改动" : "");
  for (const control of document.querySelectorAll("[data-mode-switch]")) {
    control.setAttribute("aria-pressed", String(control.dataset.modeSwitch === state.mode));
  }
  for (const control of document.querySelectorAll("[data-modes]")) {
    control.hidden = !control.dataset.modes.split(" ").includes(state.mode);
  }
  $("model-button").hidden = state.mode === "swap";
  renderConnection();
  if (!state.project) {
    const intros = {
      drama: ["开启 AI 剧项目", "新建项目后上传剧本，或直接添加人物与镜头。"],
      mtv: ["开启 AI-MTV 项目", "新建项目后导入歌曲、歌词和人物参考，安排原音时间轴。"],
      swap: ["开启 AI 换脸视频项目", "新建项目后上传视频，按识别出的人脸指定各自的参考图。"],
    };
    work.replaceChildren(title(...intros[state.mode]));
    renderJobs();
    return;
  }
  const page = {script: scriptPage, mtv: () => mtvPage(), cast: castPage, shot: shotPage, export: exportPage,
    swap: () => faceSwapPage()}[state.page]();
  work.replaceChildren(...page);
  renderJobs();
}

function mostRecent(kind, owner) {
  return [...state.jobs].reverse().find(job => job.kind === kind && job.status === "done" && (!owner || job.owner === owner));
}

function candidates(owner, kinds) {
  return state.jobs.filter(job => job.owner === owner && job.status === "done" && kinds.includes(job.kind) && job.result.file);
}

function mediaURL(file) {
  return endpoint("/media/" + encodeURIComponent(file));
}

function mediaCards(jobs, selected, action, label) {
  if (!jobs.length) return note("暂无候选");
  return node("div", {class: "media-grid"}, jobs.map((job, index) => {
    const file = job.result.file;
    const active = selected === file;
    const visual = file.endsWith(".mp4")
      ? node("video", {src: mediaURL(file), controls: true, preload: "metadata", playsinline: true, "aria-label": "候选视频 " + (index + 1)})
      : node("img", {src: mediaURL(file), loading: "lazy", alt: "候选图片 " + (index + 1)});
    return node("figure", {class: "media-tile" + (active ? " active" : "")},
      visual,
      node("figcaption", {}, "V" + String(index + 1).padStart(2, "0"),
        job.result.size ? " · " + job.result.size : "",
        job.result.duration ? " · " + job.result.duration.toFixed(2) + " 秒" : ""),
      action ? button(active ? "已采用" : label, () => action(file)) : null);
  }));
}

function adopt(object, key, file) {
  update(object, key, file);
  render();
  return saveProject();
}

async function start(kind, data = {}) {
  if (state.busy) throw Error("请等待当前任务完成。");
  const project = state.project;
  const jobsURL = endpoint("/jobs");
  state.busy = true;
  renderJobs();
  try {
    await saveProject();
    const job = await api(jobsURL, {kind, ...data});
    if (project === state.project && !state.jobs.some(item => item.id === job.id)) state.jobs.push(job);
    notice("已提交" + jobNames[kind] + "。");
    state.monitorOpen = true;
    refreshTask();
  } finally {
    state.busy = false;
    renderJobs();
    schedulePoll();
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (state.jobs.some(job => job.status === "running")) pollTimer = setTimeout(poll, 2000);
}

async function poll() {
  const project = state.project;
  try {
    const jobs = await api(endpoint("/jobs"));
    if (project !== state.project) return;
    const completed = jobs.filter(job => job.status !== "running" && state.jobs.some(old => old.id === job.id && old.status === "running"));
    state.jobs = jobs;
    if (completed.length) {
      for (const item of completed) {
        if (item.status === "done") notifyCompletion(item.id);
      }
      const last = completed.at(-1);
      if (last.status === "failed") notice(last.error, true);
      else if (last.kind === "export") notice("已保存 " + last.result.files.length + " 条镜头至 " + last.result.directory);
      else if (last.kind === "face-export") notice("已保存到 " + last.result.file);
      else notice(jobNames[last.kind] + "已完成。");
      render();
    } else renderJobs();
  } catch (error) {
    if (project === state.project) notice(error.message, true);
  } finally {
    schedulePoll();
  }
}

function renderJobs() {
  $("activity").hidden = !state.jobs.length;
  const labels = {running: "处理中", done: "完成", failed: "失败"};
  $("job-list").replaceChildren(...state.jobs.slice(-8).reverse().map(job =>
    node("p", {class: job.status === "failed" ? "subtle error" : "subtle"},
      job.created.slice(11) + " · " + jobNames[job.kind] + " · " + labels[job.status],
      job.error ? " · " + job.error : "",
      job.status === "failed" && ["cast-image", "first-image", "video", "export", "face-scan", "face-replace"].includes(job.kind)
        ? node("a", {href: endpoint("/log/" + job.id), target: "_blank", rel: "noopener"}, " 查看记录") : null)));
}

function scriptPage() {
  const project = state.project;
  const draft = mostRecent("breakdown");
  const source = panel(node("h3", {}, "剧本原文"),
    field("上传 TXT 或 Word", uploadControl(".txt,.md,.docx", async file => {
      const result = await api(endpoint("/script?filename=" + encodeURIComponent(file.name)), file);
      update(project, "script", result.script);
      render();
    }, "上传后可在下方修改正文。")),
    field("正文", inputControl(project, "script", "粘贴剧本或上传文件", 7)),
    row(button("拆分剧本", () => start("breakdown"), {
      primary: true, disabled: !textModelReady(), help: "生成角色、首帧提示词和逐镜动作草稿。看到草稿后再决定是否加入。"
    }), node("span", {class: "subtle"}, textModelLabel())));
  const heading = row(node("h3", {}, "分镜提示词"), button("添加镜头", () => {
    const shot = newShot();
    update(project, "shots", [...project.shots, shot]);
    render();
  }));
  const cards = project.shots.map((shot, index) => panel(
    row(node("h3", {}, "镜头 " + String(index + 1).padStart(2, "0")),
      button("制作此镜", () => {state.shotId = shot.id; go("shot");})),
    node("div", {class: "grid"}, field("镜头名称", inputControl(shot, "name", "镜头名称")), durationField(shot)),
    field("分镜动作提示词", inputControl(shot, "brief", "描述景别、动作、对白和镜头节奏", 3)),
    field("首帧生图提示词", inputControl(shot, "imagePrompt", "描述初始构图、人物站位和光线", 3)),
    field("H3 视频提示词", inputControl(shot, "prompt", "到镜头工作台选择参考素材后，让 Splash 起草", 4))));
  return [title("剧本与分镜", "逐镜查看和修改提示词，镜头工作台会直接使用这里的内容。"),
    source, draft ? breakdownResult(draft) : null, panel(heading), ...cards].filter(Boolean);
}

function breakdownResult(draft) {
  const result = draft.result;
  const details = node("details", {class: "panel"},
    node("summary", {}, "分镜草稿 · " + result.shots.length + " 镜"),
    ...result.shots.map((shot, i) => note((i + 1) + ". " + shot.name + " · " + shot.seconds + " 秒")),
    button("将草稿加入项目", async () => {
      const project = state.project;
      if (project.breakdown === draft.id) return;
      const casts = [...project.cast];
      for (const item of result.cast) {
        if (!casts.some(person => person.name === item.name)) casts.push(newCast(item.name, item.prompt));
      }
      const newShots = result.shots.map(item => {
        const shot = newShot(item);
        shot.refs = (Array.isArray(item.characters) ? item.characters : [])
          .flatMap(name => casts.filter(person => person.name === name).map(person => person.id));
        return shot;
      });
      project.breakdown = draft.id;
      update(project, "cast", casts);
      update(project, "shots", [...project.shots, ...newShots]);
      await saveProject();
      render();
    }, {primary: true, disabled: state.project.breakdown === draft.id}));
  return details;
}

async function uploadImage(owner, kind, file) {
  const project = state.project;
  const uploadURL = endpoint("/upload/" + owner + "?purpose=" + kind);
  await saveProject();
  const job = await api(uploadURL, file);
  if (project === state.project) {
    state.jobs.push(job);
    render();
  }
}

function modelPicker(object, key, capability, label) {
  const options = state.capabilities[capability] || [];
  const fallback = options[0]?.id || "";
  const selected = () => options.some(option => option.id === object[key]) ? object[key] : fallback;
  const select = selectControl(options.length ? options.map(option => [option.id, option.name]) : [["", "无已安装模型"]],
    selected(), value => { update(object, key, value); render(); },
    "从 Valtz 当前能力清单读取，只列出已安装的模型。");
  return {control: field(label, select), selected};
}

function castPage() {
  const project = state.project;
  return [title("人物定妆", "上传你想参考的脸，也可以先生成几张造型，再选一张在各镜头复用。"),
    panel(button("添加人物", () => {update(project, "cast", [...project.cast, newCast()]); render();}, {primary: true})),
    ...project.cast.map(castCard)];
}

function castCard(person) {
  const source = candidates(person.id, ["cast-source"]);
  const selectedSource = person.faceSource || source.at(-1)?.result.file;
  const refs = person.useSources !== false && selectedSource ? [{file: selectedSource, name: person.name}] : [];
  const model = modelPicker(person, refs.length ? "editModel" : "imageModel", refs.length ? "image-edit" : "text-to-image", "生图模型");
  return panel(
    field("人物名称", inputControl(person, "name", "角色名")),
    field("上传脸部参考 · PNG / JPEG", uploadControl("image/png,image/jpeg",
      file => uploadImage(person.id, "cast-source", file),
      "可以上传明星或其他人物的清晰参考照。图片留在当前项目中。")),
    source.length ? mediaCards(source, selectedSource, file => adopt(person, "faceSource", file), "用作脸部参考") : null,
    source.length ? node("label", {class: "check"}, node("input", {type: "checkbox", checked: person.useSources !== false,
      onchange: event => {update(person, "useSources", event.target.checked); render();}}), "生图时使用所选脸部参考") : null,
    field("定妆提示词", inputControl(person, "prompt", "写明保留的五官、发型、服装与画面风格", 3)),
    model.control,
    row(button("生成定妆候选", () => start("cast-image", {owner: person.id, prompt: person.prompt, refs, model: model.selected()}),
      {primary: true, disabled: !model.selected(), help: "每次生成新候选，保留已经生成的造型。"}),
    selectedSource ? button("直接采用这张参考照", () => adopt(person, "adopted", selectedSource)) : null),
    node("h4", {}, "定妆候选"),
    mediaCards(candidates(person.id, ["cast-image"]), person.adopted, file => adopt(person, "adopted", file), "采用造型"),
    person.adopted ? node("div", {class: "ref-list"}, node("figure", {},
      node("img", {src: mediaURL(person.adopted), alt: "当前采用造型"}), node("figcaption", {class: "subtle"}, "当前采用"))) : null);
}

function durationField(shot, refresh) {
  const options = Array.from({length: 27}, (_, i) => {
    const value = 2 + i / 2;
    return [String(value), value + " 秒"];
  });
  if (shot.mtv && !options.some(([value]) => Number(value) === Number(shot.seconds))) {
    options.push([String(shot.seconds), Number(shot.seconds).toFixed(3) + " 秒 · 原音区间"]);
    options.sort((a, b) => Number(a[0]) - Number(b[0]));
  }
  const picker = selectControl(options, shot.seconds, value => {
    update(shot, "seconds", Number(value));
    if (refresh) refresh();
  }, "按半秒选择，H3 会对齐到实际支持的帧数。");
  return field("镜头时长 · 2–15 秒", picker);
}

function frameInfo(shot) {
  const seconds = Number(shot.seconds);
  if (!(seconds >= 2 && seconds <= 15)) return "请输入 2–15 秒。";
  const frames = 5 + 17 * Math.min(20, Math.max(1, Math.round((seconds * 24 - 5) / 17)));
  return "预计生成 " + (frames / 24).toFixed(2) + " 秒 · " + frames + " 帧 · 24 fps" +
    (seconds < 4 ? " · 低于 H3 写作指南建议时长" : "");
}

function activeReferences(shot) {
  return (shot.refs || []).map(id => state.project.cast.find(person => person.id === id))
    .filter(person => person?.adopted).slice(0, 6).map(person => ({file: person.adopted, name: person.name}));
}

function shotRequest(shot) {
  const shortEdge = Math.min(...shot.size.split("x").map(Number));
  return {
    owner: shot.id, mode: shot.mode, ratio: shot.ratio, size: shot.size, seconds: shot.seconds,
    first: shot.first, last: shot.last, refs: activeReferences(shot), image_prompt: shot.imagePrompt,
    prompt: shot.prompt || shot.brief,
    quality: shot.quality === "quick" ? "balanced" : shot.quality,
    refine: shortEdge > 768 ? shot.refine ?? (shot.regenerate ? "on" : "off") : "off",
  };
}

function shotPage() {
  const shots = state.project.shots;
  if (!shots.length) return [title("镜头工作台", "从剧本页添加一个镜头，即可开始挑选首帧和制作视频。")];
  const shot = shots.find(item => item.id === state.shotId) || shots[0];
  const chooser = selectControl(shots.map((item, i) => [item.id, String(i + 1).padStart(2, "0") + " " + item.name]),
    shot.id, id => {state.shotId = id; render();}, "切换到另一镜，所有候选按镜头保留。");
  const adopted = state.project.cast.filter(person => person.adopted);
  const refs = activeReferences(shot);
  const imageModel = modelPicker(shot, refs.length ? "editModel" : "imageModel", refs.length ? "image-edit" : "text-to-image", "首帧生图模型");
  const firstFrames = candidates(shot.id, ["first-image", "first-upload"]);
  const lastFrames = candidates(shot.id, ["last-upload"]);
  const referenceInputs = adopted.map(person => node("label", {class: "check"}, node("input", {
    type: "checkbox", checked: shot.refs.includes(person.id), onchange: event => {
      const selected = new Set(shot.refs);
      if (event.target.checked) {
        if (selected.size >= 6) {
          event.target.checked = false;
          notice("V0.1 每镜最多选择 6 张参考图。", true);
          return;
        }
        selected.add(person.id);
      } else selected.delete(person.id);
      update(shot, "refs", [...selected]);
      render();
    },
  }), person.name));
  const source = panel(node("h3", {}, "构图与参考"),
    adopted.length ? field("使用已采用的定妆", node("div", {class: "row"}, referenceInputs))
      : note("可前往人物定妆采用参考图，也可直接上传镜头首帧。"),
    refs.length ? node("div", {class: "ref-list"}, refs.map((ref, i) => node("figure", {},
      node("img", {src: mediaURL(ref.file), alt: ref.name}),
      node("figcaption", {class: "subtle"}, "图 " + (i + 1) + " · " + ref.name)))) : null,
    field("镜头首帧提示词", inputControl(shot, "imagePrompt", "写明人物在画面中的位置、景别与光线", 3)),
    imageModel.control,
    button("生成首帧候选", () => start("first-image", {owner: shot.id, prompt: shot.imagePrompt, refs, ratio: shot.ratio,
      model: imageModel.selected()}), {primary: true, disabled: !imageModel.selected()}),
    field("上传镜头首帧", uploadControl("image/png,image/jpeg", file => uploadImage(shot.id, "first-upload", file))),
    mediaCards(firstFrames, shot.first, file => adopt(shot, "first", file), "采用首帧"),
    shot.mode === "FL2VA" ? node("details", {},
      node("summary", {}, shot.last ? "尾帧已采用" : "添加尾帧（可选）"),
      field("上传尾帧", uploadControl("image/png,image/jpeg", file => uploadImage(shot.id, "last-upload", file))),
      mediaCards(lastFrames, shot.last, file => adopt(shot, "last", file), "采用尾帧"),
      shot.last ? button("取消尾帧", () => adopt(shot, "last", "")) : null) : null);

  const models = state.capabilities[shot.mode === "FL2VA" ? "image-to-video" : "reference-to-video"] || [];
  const expected = shot.mode === "FL2VA" ? "minimax-h3-fl2va" : "minimax-h3-ref2va";
  const videoModel = models.find(model => model.id === expected);
  const profiles = state.profiles[shot.mode] || [];
  const wanted = shot.quality === "quick" ? "balanced" : shot.quality;
  const chosenProfile = profiles.find(profile => profile.id === wanted);
  const idAndLabel = profiles.map(profile => [profile.id, profile.name + " · " + profile.steps + " 步"]);
  if (!chosenProfile) idAndLabel.unshift(["", "请选择当前可用档位"]);
  const shortEdge = Math.min(...shot.size.split("x").map(Number));
  const refinerReady = (state.capabilities["upscale-video"] || []).some(model => model.id.startsWith("open-h3-"));
  const canRefine = refinerReady && shortEdge > 768;
  const supportedSizes = state.sizes[shot.ratio] || [];
  const duration = note(frameInfo(shot));
  const configuration = panel(node("h3", {}, "生成设置"),
    shot.mtv ? note("原音区间 " + mtvTime(shot.mtv.start_ms) + " → " + mtvTime(shot.mtv.end_ms)) : null,
    field("视频模式", selectControl([["FL2VA", "FL2VA · 首帧 / 首尾帧"], ["Ref2VA", "Ref2VA · 参考图"]], shot.mode,
      value => {update(shot, "mode", value); render();},
      "FL2VA 从选定首帧开始，可加尾帧。Ref2VA 使用定妆参考并由提示词决定构图。")),
    shot.mode === "FL2VA" ? note(shot.last ? "H3 写法：FL2VA 首尾帧" : "H3 写法：I2VA 单首帧") : note("H3 写法：Ref2VA 六段提示词"),
    node("div", {class: "grid"},
      field("画面比例", selectControl(Object.keys(state.sizes).map(value => [value, value]), shot.ratio, value => {
        update(shot, "ratio", value); update(shot, "size", state.sizes[value][0]); render();
      })),
      field("生成尺寸", selectControl(supportedSizes.map(value => [value, value.replace("x", " × ")]), shot.size,
        value => {update(shot, "size", value); render();}, "按所选比例提供生成尺寸。"))),
    durationField(shot, () => {duration.textContent = frameInfo(shot);}), duration,
    note("模型精度 · " + (videoModel?.precision || "模型未就绪")),
    field("生成档位", selectControl(idAndLabel, chosenProfile?.id ?? "",
      value => {update(shot, "quality", value); render();}, "由当前 Valtz 读取实际步数与已安装扩展。")),
    chosenProfile ? note(chosenProfile.description) : null,
    field("高分辨率精修", selectControl(
      canRefine ? [["auto", "交给 Valtz 自动决定"], ["on", "强制精修"], ["off", "直接生成"]]
        : [["off", refinerReady ? "短边超过 768 时可选" : "未安装可用精修模型"]],
      canRefine ? shot.refine ?? (shot.regenerate ? "on" : "off") : "off",
      value => update(shot, "refine", value), "精修是独立步骤，可和任一可用生成档位搭配。")),
    button("生成视频候选", () => start("video", {...shotRequest(shot), model: videoModel?.id}),
      {primary: true, disabled: !videoModel || !chosenProfile, help: "以当前提示词和输入生成一条新候选。已有候选会保留。"}));

  const draft = mostRecent("rewrite", shot.id);
  const prompts = panel(node("h3", {}, "视频提示词"),
    shot.mtv ? note("剪辑提示：" + shot.mtv.cue + (shot.mtv.kind === "performance" ? " · 表演镜头，口型需另验" : "")) : null,
    field("分镜动作草稿", inputControl(shot, "brief", "写明动作与原文对白", 3)),
    row(button("按官方 H3 技能起草", () => start("rewrite", {...shotRequest(shot), prompt: shot.brief || shot.prompt}),
      {disabled: !textModelReady(), help: "按已安装的 MiniMax H3 指南、首尾帧、参考图顺序及镜头时长起草。"}),
    node("span", {class: "subtle"}, textModelLabel())),
    draft ? node("details", {class: "panel"}, node("summary", {}, "查看草稿 · " + draft.result.official_mode),
      field("生成的草稿", node("textarea", {rows: 7, readonly: true, value: draft.result.prompt})),
      button("采用这份草稿", async () => {
        if (shot.prompt && shot.prompt !== draft.result.prompt && !confirm("用这份草稿替换当前镜头的视频提示词？")) return;
        update(shot, "prompt", draft.result.prompt);
        await saveProject();
        render();
      })) : null,
    field("当前视频提示词", inputControl(shot, "prompt", "直接输入，也可采用 Splash 的草稿", 6,
      "此处内容可编辑，并与剧本与分镜页同步。为空时，生成使用分镜动作草稿。")));
  const videos = panel(node("h3", {}, "视频候选"),
    mediaCards(candidates(shot.id, ["video"]), shot.selected, file => adopt(shot, "selected", file), "采用这条"));
  return [title("镜头工作台", "先定参考和构图，再逐镜抽卡。"), field("当前镜头", chooser),
    node("div", {class: "tools"}, node("div", {}, source, prompts), configuration), videos];
}

function exportPage() {
  const selected = state.project.shots.filter(shot => shot.selected);
  const list = selected.map(shot => node("li", {}, shot.name, " · ",
    node("a", {href: mediaURL(shot.selected), target: "_blank", rel: "noopener"}, "查看采用版"), " ",
    button("取消采用", () => adopt(shot, "selected", ""))));
  const exported = mostRecent("export");
  return [title("选片与导出", "每镜分别保存，供 DaVinci Resolve 剪辑。"),
    panel(node("h3", {}, "已选 " + selected.length + " 条"),
      list.length ? node("ol", {}, list) : note("先在镜头工作台选择满意的视频候选。"),
      field("输出格式", selectControl([["h264", "MP4 · H.264"], ["prores422hq", "MOV · ProRes 422 HQ"]],
        state.exporting, value => {state.exporting = value;}, "ProRes 适合继续剪辑，文件通常更大。")),
      note("保存位置：下载 / Valtz 导演操作台 / 当前项目"),
      button("导出选中镜头", () => start("export", {format: state.exporting}), {primary: true, disabled: !selected.length}),
      exported ? note("最近导出：" + exported.result.directory) : null)];
}

function textModelReady() {
  return state.textModel?.mode === "custom" || state.splash;
}

function textModelLabel() {
  return state.textModel?.mode === "custom" ? "API · " + state.textModel.model
    : "Splash · " + (state.textModel?.model || "Qwen3.8 27B");
}

function renderConnection() {
  if (state.mode === "swap") {
    $("connection").textContent = state.faceReady ? "本机换脸引擎可用" : "本机换脸引擎未就绪";
    return;
  }
  const version = state.native ? "Valtz " + state.native.version + " (" + state.native.build + ")" : state.valtzError || "Valtz";
  $("connection").textContent = version + " · " + (textModelReady() ? textModelLabel() : "Splash 未连接");
}

function showModelFields() {
  $("model-fields").hidden = $("model-mode").value !== "custom";
  $("model-status").textContent = "";
}

function readModelFields() {
  return {mode: $("model-mode").value, url: $("model-url").value, model: $("model-id").value,
    key: $("model-key").value, clear_key: $("model-clear-key").checked};
}

async function openModelDialog() {
  state.textModel = await api("/api/model");
  const {mode, custom} = state.textModel;
  $("model-mode").value = mode;
  $("model-url").value = custom.url;
  $("model-id").value = custom.model;
  $("model-key").value = "";
  $("model-key").placeholder = custom.has_key ? "本次运行中已有密钥" : "服务需要时填写";
  $("model-clear-key").checked = false;
  showModelFields();
  $("model-dialog").showModal();
}

const phaseLabels = {
  "encoding references": "编码参考素材", denoise: "生成画面", "vae decode": "解码画面",
  "save video": "写入视频", export: "导出文件", "text encode": "编码提示词",
  "face scan": "识别人脸", "face replacement": "逐帧换脸", "copy source audio": "合回原音",
};
let pipBody = null;
let monitorExpanded = true;
let monitorRequest = false;
let monitorSignature = "";
let toneContext;
let lastTone = "";
const tonePreference = "valtz-director-completion-sound";
const toneHistory = "valtz-director-last-completion-sound";
let soundEnabled = true;
try {soundEnabled = localStorage.getItem(tonePreference) !== "off";} catch {}

function unlockTone() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!soundEnabled || !Context) return;
  if (!toneContext) toneContext = new Context();
  if (toneContext.state === "suspended") toneContext.resume().catch(() => {});
}

function playDingDong() {
  if (toneContext?.state !== "running") return;
  const time = toneContext.currentTime;
  for (const [frequency, offset] of [[880, 0], [659.25, 0.165]]) {
    const start = time + offset;
    const note = toneContext.createOscillator();
    const volume = toneContext.createGain();
    note.type = "sine";
    note.frequency.value = frequency;
    volume.gain.setValueAtTime(0.001, start);
    volume.gain.exponentialRampToValueAtTime(0.17, start + 0.015);
    volume.gain.exponentialRampToValueAtTime(0.001, start + 0.31);
    note.connect(volume).connect(toneContext.destination);
    note.start(start);
    note.stop(start + 0.33);
  }
}

function notifyCompletion(id) {
  if (!soundEnabled || toneContext?.state !== "running") return;
  const once = () => {
    if (lastTone === id) return;
    try {
      if (localStorage.getItem(toneHistory) === id) return;
      localStorage.setItem(toneHistory, id);
    } catch {}
    lastTone = id;
    playDingDong();
  };
  if (navigator.locks) navigator.locks.request("valtz-director-completion", once).catch(() => once());
  else once();
}

function renderSound() {
  $("sound-monitor").textContent = soundEnabled ? "提示音开" : "提示音关";
  $("sound-monitor").setAttribute("aria-pressed", String(soundEnabled));
}

function fillMonitor(body) {
  const task = state.task;
  if (!task) {
    body.replaceChildren(note("暂无生成任务。"));
    return;
  }
  const stateLabel = {running: "处理中", done: "已完成", failed: "失败"}[task.status];
  const stage = task.progress;
  const children = [node("strong", {}, jobNames[task.kind] + " · " + stateLabel),
    note([task.project.name, task.owner_name].filter(Boolean).join(" / "))];
  if (task.status === "running") {
    const label = stage ? (phaseLabels[stage.stage] || stage.stage) + " · 阶段 " + stage.percent + "%" : "等待模型返回进度";
    children.push(node("progress", {max: 100, value: stage?.percent, "aria-label": label}), note(label));
  }
  if (task.error) children.push(node("p", {class: "note error"}, task.error));
  body.replaceChildren(...children);
}

function renderMonitor() {
  $("monitor").hidden = !state.monitorOpen;
  $("monitor-body").hidden = !monitorExpanded;
  $("collapse-monitor").textContent = monitorExpanded ? "收起" : "展开";
  $("collapse-monitor").setAttribute("aria-expanded", String(monitorExpanded));
  const signature = JSON.stringify(state.task);
  if (signature !== monitorSignature) {
    fillMonitor($("monitor-body"));
    if (pipBody) fillMonitor(pipBody);
    monitorSignature = signature;
  }
}

async function refreshTask() {
  if (monitorRequest) return;
  monitorRequest = true;
  try {
    const task = await api("/api/task-status");
    if (task?.status === "running" && task.id !== state.task?.id) {
      state.monitorOpen = true;
      monitorExpanded = true;
    }
    if (task?.status === "done" && state.task?.id === task.id && state.task.status === "running") {
      notifyCompletion(task.id);
    }
    state.task = task;
    renderMonitor();
  } catch {
    // A lost poll isn't proof that the native task stopped.
  } finally {
    monitorRequest = false;
  }
}

async function pinMonitor() {
  const floating = await window.documentPictureInPicture.requestWindow({width: 350, height: 160});
  const doc = floating.document;
  const stylesheet = doc.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = new URL("/app.css", location.href).href;
  doc.head.append(stylesheet);
  doc.title = "Valtz · 任务进度";
  doc.documentElement.lang = "zh-CN";
  doc.body.className = "pip-shell";
  pipBody = doc.createElement("div");
  pipBody.className = "monitor-body";
  pipBody.setAttribute("aria-live", "polite");
  doc.body.append(pipBody);
  fillMonitor(pipBody);
  floating.setInterval(refreshTask, 2500);
  floating.addEventListener("pagehide", () => {pipBody = null;}, {once: true});
}

$("new-project").addEventListener("submit", event => {
  event.preventDefault();
  perform(async () => {
    await saveProject();
    const project = await api("/api/projects", {name: $("project-title").value.trim(), mode: state.mode});
    $("project-title").value = "";
    await reloadProjects();
    await openProject(project.id);
  });
});
$("project").addEventListener("change", event => perform(() => openProject(event.target.value)));
$("save").addEventListener("click", () => perform(() => saveProject(true)));
$("refresh").addEventListener("click", () => perform(reloadCapabilities));
$("model-button").addEventListener("click", () => perform(openModelDialog));
$("model-close").addEventListener("click", () => $("model-dialog").close());
$("model-mode").addEventListener("change", showModelFields);
$("model-test").addEventListener("click", async () => {
  $("model-status").textContent = "正在请求模型列表…";
  try {
    const result = await api("/api/model/test", readModelFields());
    $("model-status").textContent = result.matched ? "连接成功，找到所选模型。"
      : "连接成功，模型列表中没有该 ID。请核对服务商提供的名称。";
  } catch (error) {
    $("model-status").textContent = error.message;
  }
});
$("model-form").addEventListener("submit", event => {
  event.preventDefault();
  perform(async () => {
    state.textModel = await api("/api/model", readModelFields());
    $("model-key").value = "";
    $("model-dialog").close();
    renderConnection();
    render();
  });
});
$("monitor-button").addEventListener("click", () => {
  state.monitorOpen = true;
  monitorExpanded = true;
  renderMonitor();
  refreshTask();
});
$("collapse-monitor").addEventListener("click", () => {
  monitorExpanded = !monitorExpanded;
  renderMonitor();
});
$("sound-monitor").addEventListener("click", () => {
  soundEnabled = !soundEnabled;
  try {localStorage.setItem(tonePreference, soundEnabled ? "on" : "off");} catch {}
  if (soundEnabled) unlockTone();
  renderSound();
});
document.addEventListener("pointerdown", unlockTone, {passive: true});
document.addEventListener("keydown", unlockTone);
if (window.documentPictureInPicture) {
  $("pin-monitor").hidden = false;
  $("pin-monitor").addEventListener("click", () => perform(pinMonitor));
}
for (const control of document.querySelectorAll("[data-page]")) {
  control.addEventListener("click", () => go(control.dataset.page));
}
for (const control of document.querySelectorAll("[data-mode-switch]")) {
  control.addEventListener("click", () => perform(() => switchMode(control.dataset.modeSwitch)));
}
window.addEventListener("beforeunload", event => {
  if (state.dirty) event.preventDefault();
});
render();
renderSound();
fillMonitor($("monitor-body"));
refreshTask();
setInterval(refreshTask, 2500);
perform(async () => {
  state.textModel = await api("/api/model");
  renderConnection();
  render();
});
perform(async () => {
  await reloadProjects();
  const latestDrama = state.projects.find(project => (project.mode || "drama") === "drama");
  if (latestDrama) await openProject(latestDrama.id);
});
perform(reloadCapabilities);
