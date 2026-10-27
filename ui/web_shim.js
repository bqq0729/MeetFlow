// pywebview 데스크톱 앱의 window.pywebview.api를 흉내 내서, 같은 ui/index.html이
// 수정 없이 웹(로컬 서버)에서도 돌아가게 한다. api.아무메서드(...) 호출을 그대로
// POST /api/아무메서드 로 보내고, 파이썬 쪽 App.onEvent(evt, data) 알림은
// /events 폴링으로 받아 그대로 흉내 낸다.
(function () {
  window.pywebview = window.pywebview || {};

  async function uploadFile(f) {
    const buf = await f.arrayBuffer();
    const r = await fetch("/upload?name=" + encodeURIComponent(f.name), { method: "POST", body: buf });
    const j = await r.json();
    return j.path || "";
  }

  // "찾아보기": 녹음·대본은 지금 접속한 컴퓨터(브라우저)의 파일을 올리고,
  // 양식·용어집·회의록 파일은 이 서버(프로젝트 폴더) 안에 미리 넣어둔 것 중에서 고른다.
  // 서버 PC에 화면이 없어도(헤드리스) 되고, 접속한 컴퓨터의 창도 아니라서 tkinter 다이얼로그는 안 쓴다.
  const UPLOAD_ACCEPT = {
    media: ".mp3,.wav,.m4a,.flac,.ogg,.aac,.wma,.mp4,.mkv,.mov,.avi,.webm,.txt",
    txt: ".txt",
  };
  const LIST_KIND = { template: "template", doc: "doc", glossary: "glossary" };
  const LIST_TITLE = { template: "회의록 양식 고르기", doc: "회의록 파일 고르기", glossary: "참고 용어집 고르기" };
  const LIST_HINT = {
    template: "서버의 templates 폴더에 .hwpx/.docx/.pdf/.md/.txt 양식 파일을 넣어두세요.",
    doc: "서버의 output 폴더에서 고릅니다 (회의록을 만들면 자동으로 여기 저장돼요).",
    glossary: "서버의 glossary 폴더에 .txt/.md/.csv 용어집 파일을 넣어두세요.",
  };

  function webUpload(what) {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = what === "media";
      input.accept = UPLOAD_ACCEPT[what] || "";
      input.style.display = "none";
      document.body.appendChild(input);
      input.addEventListener("change", async () => {
        const files = [...input.files];
        document.body.removeChild(input);
        if (!files.length) return resolve("");
        const paths = [];
        for (const f of files) {
          try {
            const p = await uploadFile(f);
            if (p) paths.push(p);
          } catch (err) { /* 업로드 실패한 파일은 건너뛴다 */ }
        }
        resolve(what === "media" ? paths : (paths[0] || ""));
      });
      input.addEventListener("cancel", () => { document.body.removeChild(input); resolve(""); });
      input.click();
    });
  }

  async function webList(kind) {
    const r = await fetch("/api/list_files", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify([LIST_KIND[kind]]),
    });
    const { dir, files } = await r.json();
    if (!files || !files.length) {
      await alertBox(LIST_TITLE[kind], `${dir}에 고를 파일이 없어요.\n${LIST_HINT[kind]}`);
      return "";
    }
    const buttons = files.map((f) => ({ label: f.name, value: f.path }));
    buttons.push({ label: "취소", value: "" });
    return (await modal(LIST_TITLE[kind], dir, buttons)) || "";
  }

  function webPick(what) {
    if (what === "folder") {
      return fetch("/api/list_files", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(["output"]),
      }).then((r) => r.json()).then((j) => j.dir || "");
    }
    if (LIST_KIND[what]) return webList(what);
    return webUpload(what);
  }

  window.pywebview.api = new Proxy({}, {
    get(_target, method) {
      if (method === "pick") return webPick;
      return (...args) => fetch("/api/" + method, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(args),
      }).then((r) => r.json());
    },
  });

  let cursor = 0;
  async function poll() {
    try {
      const r = await fetch("/events?since=" + cursor);
      const { events, next } = await r.json();
      cursor = next;
      for (const e of events) {
        if (window.App && window.App.onEvent) window.App.onEvent(e.event, e.data);
      }
    } catch (e) { /* 서버가 잠깐 안 떠 있어도 다음 폴링에서 다시 시도 */ }
    setTimeout(poll, 350);
  }
  poll();

  window.dispatchEvent(new Event("pywebviewready"));

  // 탐색기에서 끌어다 놓기: 데스크톱 앱은 OS 수준 드롭 훅으로 경로를 받지만,
  // 브라우저는 파일 내용만 주므로 서버에 업로드해서 로컬 경로로 바꿔 돌려준다.
  window.addEventListener("drop", async (e) => {
    e.preventDefault();
    const files = [...(e.dataTransfer && e.dataTransfer.files || [])];
    if (!files.length) return;
    const paths = [];
    for (const f of files) {
      try {
        const buf = await f.arrayBuffer();
        const r = await fetch("/upload?name=" + encodeURIComponent(f.name), { method: "POST", body: buf });
        const j = await r.json();
        if (j.path) paths.push(j.path);
      } catch (err) { /* 업로드 실패한 파일은 건너뛴다 */ }
    }
    if (paths.length && window.App && window.App.routeDrop) window.App.routeDrop(paths);
  }, true);
})();
