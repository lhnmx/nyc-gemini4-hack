import { useLayoutEffect, useRef, useState } from 'react';
import { sourcePreviewLinks } from './sourcePreview.js';
import './sourcePlayer.css';

let pendingApi;

function loadPlayerApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (pendingApi) return pendingApi;
  pendingApi = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const previous = window.onYouTubeIframeAPIReady;
    const timeout = window.setTimeout(failed, 15000);
    function restore() {
      window.clearTimeout(timeout);
      if (window.onYouTubeIframeAPIReady === ready) window.onYouTubeIframeAPIReady = previous;
    }
    function failed() {
      restore();
      script.remove();
      pendingApi = null;
      reject(new Error('YouTube player could not load'));
    }
    function ready() {
      if (!window.YT?.Player) return failed();
      restore();
      resolve(window.YT);
      previous?.();
    }
    window.onYouTubeIframeAPIReady = ready;
    script.onerror = failed;
    script.src = 'https://www.youtube.com/iframe_api';
    document.head.append(script);
  });
  return pendingApi;
}

function errorMessage(code) {
  if (code === 101 || code === 150) return 'This tutorial can only be watched on YouTube.';
  if (code === 153) return 'YouTube couldn’t verify playback in this browser.';
  if (code === 100) return 'YouTube reports this video is private or has been removed.';
  return 'YouTube couldn’t play this preview here.';
}

export default function LoopingSourceClip({ url, segment, title, iframeId, loop = true }) {
  const host = useRef(null);
  const [failure, setFailure] = useState(null);
  const links = sourcePreviewLinks(url, segment);
  const embed = links?.embed;
  const start = segment?.start_seconds;
  const end = segment?.end_seconds;

  useLayoutEffect(() => {
    if (!embed) return;
    const container = host.current;
    const iframe = document.createElement('iframe');
    const src = new URL(embed);
    src.searchParams.set('enablejsapi', '1');
    src.searchParams.set('origin', window.location.origin);
    src.searchParams.set('autoplay', '0');
    iframe.referrerPolicy = 'strict-origin-when-cross-origin';
    iframe.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    iframe.allowFullscreen = true;
    if (iframeId) iframe.id = iframeId;
    iframe.src = src.href;
    container.replaceChildren(iframe);
    setFailure(null);
    const videoId = src.pathname.split('/').at(-1);
    const video = start == null ? { videoId } : { videoId, startSeconds: start, endSeconds: end };
    let active = true;
    let player;
    // A blocked embed can leave a blank frame without emitting a YouTube error.
    const timeout = window.setTimeout(() => fail(), 20000);
    function fail(code) {
      if (!active) return;
      active = false;
      window.clearTimeout(timeout);
      setFailure({ embed, message: errorMessage(code) });
      container.replaceChildren();
    }
    loadPlayerApi().then(YT => {
      if (!active) return;
      player = new YT.Player(iframe, { events: {
        onReady({ target }) {
          if (!active) return;
          window.clearTimeout(timeout);
          if (loop) target.mute();
          target.playVideo();
        },
        onStateChange({ data, target }) {
          // Reload the bounded selection. seekTo() discards YouTube's configured endSeconds.
          if (active && loop && data === YT.PlayerState.ENDED) target.loadVideoById(video);
        },
        onError({ data }) { fail(data); },
      } });
    }).catch(() => fail());
    return () => {
      active = false;
      window.clearTimeout(timeout);
      player?.destroy();
      container.replaceChildren();
    };
  }, [embed, start, end, iframeId, loop]);

  useLayoutEffect(() => {
    const frame = host.current?.querySelector('iframe');
    if (frame) frame.title = title;
  }, [title, embed, iframeId, loop]);

  if (!links) return null;
  const message = failure?.embed === embed && failure.message;
  return <div className="source-player">
    <div ref={host} className="source-player-frame" hidden={Boolean(message)} />
    {message && <div className="source-player-error" role="status">
      <p>{message}</p>
      <a href={links.href} target="_blank" rel="noopener noreferrer">Open on YouTube ↗</a>
    </div>}
  </div>;
}
