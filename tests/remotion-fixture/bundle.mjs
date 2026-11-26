// node bundle.mjs <out dir>: `remotion bundle` equivalent for the fixture
import { bundle } from "@remotion/bundler";
const out = await bundle({ entryPoint: new URL("./src/index.jsx", import.meta.url).pathname, outDir: process.argv[2],
                           publicDir: new URL("./public", import.meta.url).pathname });
console.log("bundled", out);
