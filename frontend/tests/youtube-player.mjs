// Runs in the test page, either before navigation or as the intercepted IFrame API script.
export function installYoutubePlayer() {
  window.youtubePlayers = [];
  class Player {
    constructor(iframe, { events }) {
      this.iframe = iframe;
      this.events = events;
      this.commands = [];
      window.youtubePlayers.push(this);
      if (!window.holdPlayerReady) queueMicrotask(() => this.ready());
    }
    ready() { this.events.onReady({ target: this }); }
    state(data) { this.events.onStateChange({ target: this, data }); }
    error(data) { this.events.onError({ target: this, data }); }
    mute() { this.commands.push('mute'); }
    playVideo() { this.commands.push('play'); }
    loadVideoById(video) { this.commands.push({ load: video }); }
    destroy() { this.iframe.remove(); }
  }
  window.YT = { Player, PlayerState: { ENDED: 0, PLAYING: 1, PAUSED: 2 } };
  window.onYouTubeIframeAPIReady?.();
}
