# Designing the CraftPOV guide

Someone with paper in both hands may need to replay a fold several times. Someone gathering supplies may just want to know how much paper to find. The guide supports both tasks on one page and makes its instructions available while the film renders.

## What the references suggest

| Reference | Observed pattern | Application to CraftPOV |
| --- | --- | --- |
| Notion | The table of contents uses the page’s actual headings. Breadcrumbs show a page’s place in its parent workspace. [Document navigation](https://www.notion.com/help/columns-headings-and-dividers) · [Page hierarchy](https://www.notion.com/help/create-a-subpage) | Use the planner’s precise step titles in both the outline and the reading area. Selecting a step should make its position and title immediately apparent. |
| Apple | Alignment, spacing and disclosure communicate hierarchy. A sidebar can yield space to the detail view as the window narrows. [Layout](https://developer.apple.com/design/human-interface-guidelines/layout) · [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars) | Give the project title, instructions and player a common edge. Keep the current step near the top on phones, with the outline within reach. Reserve emphasis for the action the maker can take now. |
| iFixit | Guide authors pair instructions with photographs that show the hands and tools carrying out the action. The introduction prepares the reader before the first step. [Creating a repair guide](https://www.ifixit.com/Info/Repair_Guide) | Put quantities in the preparation checklist. Pair each chapter of the film with an instruction and an observable result the maker can compare with their work. |
| Apple and W3C | Apple favors familiar playback controls and the original aspect ratio. W3C places the transcript near its video and calls out visual details that a listener may never hear. [Playing video](https://developer.apple.com/design/human-interface-guidelines/playing-video) · [Transcripts](https://www.w3.org/WAI/media/av/transcripts/) | Fit the entire workpiece in view. Keep the narration transcript close to playback and label hand movements and verification cues separately. A maker can then check an edge or seam with the sound off. |

## GitHub components worth studying

- **shadcn/ui sidebar.** The component uses independent mobile open state, a sheet on small screens, and an active state for each menu item. A short guide can use an in-page outline and ordinary buttons. A sheet is a useful alternative when longer lists crowd the player, though it adds a step to reach another chapter. [Sidebar source](https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/sidebar.tsx)
- **Mux Media Chrome.** Its plain HTML example reserves the video’s space before loading and connects play, time, volume and fullscreen controls to one media element. CraftPOV keeps its full film in one native player. Selecting a chapter seeks to its start while preserving the maker’s playback and caption choices. [Vanilla player example](https://github.com/muxinc/media-chrome/blob/main/examples/vanilla/basic.html)
- **Radix Primitives.** Its accordion connects triggers and panels by ID and supports directional keyboard navigation for groups. For a single narration disclosure, native `details` and `summary` provide a labeled, keyboard-operable toggle. Keep the physical instruction visible with the disclosure closed. [Accordion source](https://github.com/radix-ui/primitives/blob/main/packages/react/accordion/src/accordion.tsx)
- **GOV.UK Frontend.** Its task-list template gives each row a text status and associates the status with the row’s link. In CraftPOV, a status message describes generation and a checked material records something the maker has gathered. [Task-list template](https://github.com/alphagov/govuk-frontend/blob/main/packages/govuk-frontend/src/govuk/components/task-list/template.njk)

## Checks against the generation workflow

The [pipeline](src/nyc_gemini4_hack/pipeline.py) publishes the written plan before rendering clips. It assembles the full film after every step has a playable clip. These scenarios connect that contract to observable interface behavior.

1. Enter a new goal and optional supplies. As soon as the plan arrives, open a later step and read its instructions, even if the first clip is still rendering.
2. Check a supply and select a later step while the film is rendering. When the server publishes the full film, the player should open at that chapter and the supply should stay checked.
3. Let the film play across a chapter boundary. Its written step should advance with it. Select another chapter while playing, then while paused. Each action should keep the same player and its playback state. If film assembly fails, the written guide and checklist should remain available.
4. At phone width, reach playback, transcript and another step without horizontal scrolling. Repeat using the keyboard, checking that focus follows an intentional step change and stays put on background updates.
