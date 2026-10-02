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

## OptionWheel (#598)

Source: https://github.com/DavidHDev/react-bits/tree/1eeb6f105c68b964289d85dabbe84a1d551f3797/src/ts-default/Components/OptionWheel
Copyright David Haz, MIT + Commons Clause (LICENSE.md). `OptionWheel.tsx` and `OptionWheel.css` are a local
adaptation for the captions of the search loading scene: the curve layout (circle radius from row height and
tilt, per-row transform, opacity, blur and colour mix) is the original; interaction is removed (no drag, click,
mouse wheel, keyboard, sound, listbox roles), the position is a controlled ever-increasing step eased over a
fixed duration, and only a window of rows around it is rendered.
