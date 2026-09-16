/* 启动：所有模块加载完之后才调用 boot()（拆分前它夹在 app.js 中间） */
boot().catch((error) => reportClientError("boot", error));
