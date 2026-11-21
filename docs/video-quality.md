# Generating instructional video that shows the action

Research reviewed on November 21, 2026. CraftPOV needs a viewer to see where to put their hands, which part moves, and how to tell when the action is complete. A continuous-looking shot can still teach the wrong action.

## How YouTube informs a chapter

The researcher gives Gemini the selected public YouTube video itself. It extracts timed observations of the technique, hand movements and visible tools. In earlier versions the planner summarized these observations into instructions and a motion prompt. The renderer lost the connection between an individual instruction and the moment in the tutorial that supported it.

Each new chapter can select identified segments from the extraction. The server resolves their IDs against the actual source records and preserves the URL, time interval and observations. Both the physical planner and the motion renderer receive those chapter-specific observations. The viewer can open **Source footage** at the original time. The generated film uses new footage. Gemini’s video-understanding documentation notes that default sampling can miss rapid changes, so a timestamped observation still needs an observable completion cue. [Google video understanding](https://aistudio.google.com/docs/video-understanding)

## Research and design choices

| Finding | How it changes a guide |
| --- | --- |
| In July’s OSCBench study, six video models could match the requested scene while misrepresenting the change to the object, especially for combined actions. [ACL paper](https://aclanthology.org/2026.acl-long.1425/) | Give each chapter an observable end condition. For a fold, name the moving layer, the crease and where the edge should land. Review the resulting shape as well as the moving hands. |
| The October Ego2Act preprint reports that egocentric models often skip intermediate manipulations, leaving the next action without its prerequisite. [Authors’ paper](https://arxiv.org/abs/2610.01092) | Split actions that change the working face, hinge or tool. Carry a reviewed final frame between dependent actions. Independent pieces can render concurrently. |
| The November HandWorldBench preprint compares hand quality, instruction following and object evolution. A convincing hand pose can accompany a mistaken object transition. [Authors’ paper](https://arxiv.org/abs/2611.24731) | Inspect the opening state, visible contact during the action and lasting result separately. A hand covering the work gives insufficient evidence to confirm a fold. |
| Omni 1.2 accepts an ordered pair of starting and ending images. Google documents a 3–10 second duration for this mode, versus up to 15 seconds for single-image generation. [API guide](https://aistudio.google.com/docs/omni#first-and-last-frame-interpolation) · [November release](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemini-omni-1-2-flash/) | When a shot clearly misses its target, supply the observed discrepancy. If the opening is sound, reuse it. A proposed end image must pass a visual check before it can guide the corrective take. |

## What the worker checks

The worker decodes images from the actual output across the shot, including its beginning and end. A separate Gemini vision call compares those samples against the chapter and its source observations. Each saved review includes the model, timestamps, findings, attempt and elapsed time.

A clearly contradicted action gets at most one corrective take. The worker withholds footage after an unsuccessful correction. A first take with insufficient evidence or an unavailable reviewer is recorded accordingly and cannot supply a verified reference frame to its child. Sampling can miss events between frames. Readers can compare a tricky movement with its linked tutorial.

## Verification

A live calibration used three short slider demonstrations with a visible physical stop. Gemini Carbon accepted two recordings where the moving piece reached the stop, including one on a different surface. It rejected the third and identified the gap left at the end. Reviews took about 10–14 seconds apiece. This small check measures those three known outcomes. It does not establish a general craft accuracy rate.

Automated tests exercise source-to-chapter attribution, sampled review, corrective first/last references, persistence, provider unavailability, cancellation and prevention of rejected-frame reuse. The separate scheduler replay uses simulated provider response times and measures overlap in local assembly. [Scheduler measurements](generation.md)
