const { createApp } = require("./app");

const port = Number(process.env.PORT || 8101);
createApp().listen(port, () => console.log(`node_app listening on ${port}`));
