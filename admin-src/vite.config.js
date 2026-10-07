import {defineConfig} from "vite"; import react from "@vitejs/plugin-react";
export default defineConfig({base:"/admin/",plugins:[react()],build:{outDir:"../admin",emptyOutDir:true},
server:{proxy:{"/api":"http://localhost:8080"}}});
