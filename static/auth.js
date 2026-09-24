(() => {
  const $ = (selector) => document.querySelector(selector);
  const params = new URLSearchParams(location.search);
  const safeNext = (() => {
    const value = params.get("next") || "/music";
    return value.startsWith("/") && !value.startsWith("//") ? value : "/music";
  })();
  let mode = location.pathname === "/register" ? "register" : "login";

  function message(text, success = false) {
    const target = $("#form-message");
    target.hidden = !text;
    target.textContent = text || "";
    target.classList.toggle("success", success);
  }

  function render() {
    const register = mode === "register";
    document.title = `${register ? "注册" : "登录"} - 声屿音乐`;
    $("#auth-title").textContent = register ? "创建你的音乐空间" : "欢迎回来";
    $("#auth-intro").textContent = register
      ? "歌单和喜欢的音乐会保存在你的账号中。"
      : "登录后继续管理你的歌单和喜欢的音乐。";
    $("#display-name-field").hidden = !register;
    $("#display-name").required = register;
    $("#password").autocomplete = register ? "new-password" : "current-password";
    $("#username").minLength = register ? 8 : 3;
    $("#username").placeholder = register ? "8-32 位英文字母或数字，区分大小写" : "请输入用户名（区分大小写）";
    $("#auth-submit").textContent = register ? "注册并进入音乐平台" : "登录";
    $("#mode-prefix").textContent = register ? "已经有账号？" : "还没有账号？";
    $("#toggle-mode").textContent = register ? "去登录" : "注册账号";
    message("");
  }

  function registrationUsernameError(username) {
    if (mode !== "register" || !username) return "";
    return /^[A-Za-z0-9]{8,32}$/.test(username)
      ? ""
      : "用户名需为 8-32 位英文字母或数字，不能包含空格、中文或特殊符号。";
  }

  $("#username").addEventListener("input", () => {
    message(registrationUsernameError($("#username").value));
  });

  $("#toggle-mode").addEventListener("click", () => {
    mode = mode === "login" ? "register" : "login";
    const path = mode === "register" ? "/register" : "/login";
    history.replaceState(null, "", `${path}${location.search}`);
    render();
  });

  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = $("#username").value;
    const password = $("#password").value;
    const displayName = $("#display-name").value.trim();
    if (!username || !password || (mode === "register" && !displayName)) {
      message("请完整填写表单。");
      return;
    }
    const usernameError = registrationUsernameError(username);
    if (usernameError) {
      message(usernameError);
      return;
    }
    const button = $("#auth-submit");
    button.disabled = true;
    button.textContent = mode === "register" ? "正在注册" : "正在登录";
    message("");
    try {
      const payload = { username, password };
      if (mode === "register") payload.display_name = displayName;
      const response = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.msg || "操作未完成");
      message(data.msg || "操作成功，正在跳转。", true);
      location.assign(safeNext);
    } catch (error) {
      message(error.message || "请求失败，请稍后重试。");
    } finally {
      button.disabled = false;
      if (document.visibilityState === "visible") button.textContent = mode === "register" ? "注册并进入音乐平台" : "登录";
    }
  });

  fetch("/api/auth/me")
    .then((response) => response.json())
    .then((data) => {
      if (data?.user) location.replace(safeNext);
    })
    .catch(() => {});
  render();
})();
