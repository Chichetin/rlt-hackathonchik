# React Bits backgrounds

Source: https://github.com/DavidHDev/react-bits/tree/e1bbb696fc53f7f91e694c529e4d68c899773b6e/src/ts-default/Backgrounds

PatternWaves and MicroSlats TypeScript/CSS were copied from the commit linked above.
Copyright David Haz; the full applicable license is in LICENSE.md and must remain with these files.
These components are used within the Mitrandir application.

Local changes: catch unavailable WebGL without crashing the application; stop/restart
requestAnimationFrame on page visibility and offscreen changes. Shaders and reference presets
are unchanged. WaveField selects the component by route and remounts it when the system motion
preference changes. A CSS texture remains when WebGL2 is unavailable. OGL is the renderer
required by these components.
