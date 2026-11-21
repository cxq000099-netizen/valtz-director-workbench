"use strict";

const swapVideos = new Map();
const swapLoading = new Set();

function swapSettings(project) {
  if (!project.swap) project.swap = {video: "", scan: "", mappings: {}};
  return project.swap;
}

function loadSwapVideo(project, key) {
  if (!key || swapVideos.has(key) || swapLoading.has(key)) return;
  swapLoading.add(key);
  api(endpoint("/swap-video/" + key)).then(video => {
    swapVideos.set(key, video);
    if (state.project === project && state.page === "swap") render();
  }).catch(error => {
    if (state.project === project) notice(error.message, true);
  }).finally(() => swapLoading.delete(key));
}

async function uploadSwapSource(project, settings, file) {
  const url = endpoint("/swap-video?filename=" + encodeURIComponent(file.name));
  notice("正在导入原视频…");
  const video = await api(url, file);
  swapVideos.set(video.id, video);
  if (state.project !== project) return;
  update(settings, "video", video.id);
  update(settings, "scan", "");
  update(settings, "mappings", {});
  await saveProject();
  render();
  notice("视频已保存。点击「识别人脸」分组。");
}

async function assignSwapReference(project, settings, scan, face, file) {
  const payload = await api(endpoint("/swap-reference"), file);
  if (state.project !== project || settings.video !== scan.result.video_id
      || mostRecent("face-scan", settings.video)?.id !== scan.id) return;
  const mapping = settings.scan === scan.id ? {...settings.mappings} : {};
  mapping[face.id] = payload.file;
  update(settings, "scan", scan.id);
  update(settings, "mappings", mapping);
  await saveProject();
  render();
}

function faceTile(project, settings, scan, face, index) {
  const ref = settings.scan === scan.id ? settings.mappings[face.id] : "";
  return node("article", {class: "swap-person"},
    node("h4", {}, "人物 " + (index + 1)),
    note("扫描命中 " + face.samples + " 次 · 样例 " + mtvTime(face.time * 1000)),
    node("div", {class: "swap-pair"},
      node("figure", {}, node("img", {src: mediaURL(face.thumbnail), alt: "视频原脸 " + (index + 1)}),
        node("figcaption", {class: "subtle"}, "视频中识别的脸")),
      node("figure", {}, ref ? node("img", {src: mediaURL(ref), alt: "用于换脸的参考图"})
          : node("div", {class: "swap-placeholder"}, note("保留原脸")),
        node("figcaption", {class: "subtle"}, "替换参考图"))),
    field("给这张脸指定参考图", uploadControl(".png,.jpg,.jpeg,image/png,image/jpeg",
      file => assignSwapReference(project, settings, scan, face, file),
      "请上传清晰的单人正脸。其他未指定的人脸保持原样。")),
    row(button("看原视频位置", () => {
      const video = $("swap-source-player");
      if (video) video.currentTime = face.time;
    }), ref ? button("取消替换", async () => {
      const mapping = {...settings.mappings};
      delete mapping[face.id];
      update(settings, "mappings", mapping);
      await saveProject();
      render();
    }) : null));
}

function faceSwapPage() {
  const project = state.project;
  const settings = swapSettings(project);
  loadSwapVideo(project, settings.video);
  const video = swapVideos.get(settings.video);
  const scan = video ? mostRecent("face-scan", video.id) : null;
  const swapJobs = video ? state.jobs.filter(job =>
    job.kind === "face-replace" && job.status === "done" && job.result.video_id === video.id) : [];
  const currentMappings = scan?.id === settings.scan ? settings.mappings : {};
  const chosen = Object.keys(currentMappings).length;
  const output = mostRecent("face-export", settings.video);
  return [title("AI 换脸视频", "给视频中每个识别出的人物分别配一张参考图。"),
    panel(node("h3", {}, "1. 上传原视频"),
      field("原视频 · MP4 / MOV", uploadControl(".mp4,.mov,video/mp4,video/quicktime",
        file => uploadSwapSource(project, settings, file))),
      video ? note(video.name + " · " + video.duration.toFixed(2) + " 秒") : null,
      video ? node("video", {id: "swap-source-player", class: "swap-source",
        src: endpoint("/swap-video/" + video.id + "/preview"), controls: true, preload: "metadata", playsinline: true}) : null,
      row(button("识别人脸", () => start("face-scan"), {primary: true, disabled: !video || !state.faceReady,
        help: "从原视频抽样分组，按脸的特征区分人物。"})),
      !state.faceReady ? note("本机换脸引擎尚未就绪。") : null),
    panel(node("h3", {}, "2. 指定替换参考图"),
      scan ? note("识别出 " + scan.result.faces.length + " 组可核对的人脸。遮挡或侧脸可能被分成多组，可为同一人使用同一张参考图。")
           : note("识别完成后，这里会出现原视频中的人脸缩略图。"),
      scan?.result.faces.length ? node("div", {class: "swap-tiles"},
        scan.result.faces.map((face, i) => faceTile(project, settings, scan, face, i))) : null),
    panel(node("h3", {}, "3. 换脸预览与导出"),
      note("已指定 " + chosen + " 张脸。其余人脸保留。"),
      row(button("试换约 5 秒", () => start("face-replace", {preview: true}), {disabled: !chosen || !state.faceReady}),
        button("生成整段换脸视频", () => start("face-replace", {preview: false}),
          {primary: true, disabled: !chosen || !state.faceReady})),
      ...swapJobs.slice().reverse().map(job => node("details", {class: "mtv-shot", open: job.id === swapJobs.at(-1)?.id},
        node("summary", {}, (job.result.preview ? "试换预览" : "整段候选") + " · " + job.result.duration.toFixed(2) + " 秒"),
        node("video", {class: "swap-source", src: mediaURL(job.result.file), controls: true, preload: "metadata", playsinline: true}),
        note("已替换 " + Object.values(job.result.counts).filter(value => value > 0).length + " 组人物，保留原视频音轨。"),
        job.result.preview ? null : button("保存到下载文件夹", () => start("face-export", {result_id: job.id})))),
      output ? note("已保存到 " + output.result.file) : null)];
}
