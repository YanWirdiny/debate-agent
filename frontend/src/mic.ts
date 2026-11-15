/**
 * Live room mic: the browser's microphone -> 16 kHz mono PCM16, 100 ms chunks -> /ws/mic.
 * The Deepgram key never reaches the browser: the backend forwards the audio.
 *
 * Browsers only give mic access on localhost or HTTPS.
 */

// Runs on the audio thread. Box-filter downsampling from the device rate (usually 48 kHz) to 16 kHz,
// then 1600-sample (100 ms) Int16 chunks, each with its RMS level for the meter.
const WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0; this.sum = 0; this.n = 0; this.sq = 0; this.i = 0;
    this.buf = new Int16Array(1600);
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let k = 0; k < ch.length; k++) {
      this.sum += ch[k]; this.n++; this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.sum / this.n));
        this.sum = 0; this.n = 0;
        this.buf[this.i++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        this.sq += s * s;
        if (this.i === this.buf.length) {
          const rms = Math.sqrt(this.sq / this.buf.length);
          this.port.postMessage({ pcm: this.buf.buffer, rms }, [this.buf.buffer]);
          this.buf = new Int16Array(1600); this.i = 0; this.sq = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);
`;

export async function listMics(): Promise<MediaDeviceInfo[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  return (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
}

export class MicStream {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private ws: WebSocket | null = null;
  private stopped = false;

  constructor(
    private onLevel: (rms: number) => void,
    private onError: (message: string) => void,
  ) {}

  /** Call from a click (browsers only start audio after a user gesture). Throws if the mic is refused. */
  async start(deviceId?: string): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("This browser cannot use the microphone here (it needs localhost or HTTPS).");
    }
    this.stopped = false;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.ctx = new AudioContext();
    await this.ctx.resume();
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    try {
      await this.ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const source = this.ctx.createMediaStreamSource(this.stream);
    const node = new AudioWorkletNode(this.ctx, "pcm16");
    const mute = this.ctx.createGain();
    mute.gain.value = 0; // keep the graph pulled without playing the mic back into the room
    source.connect(node).connect(mute).connect(this.ctx.destination);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = (this.ws = new WebSocket(`${proto}://${location.host}/ws/mic`));
    ws.binaryType = "arraybuffer";
    ws.onclose = (e) => {
      if (this.stopped) return;
      this.onError(e.code === 1013 ? "Another microphone is already connected to the backend." : "Microphone connection to the backend closed.");
    };
    node.port.onmessage = (e: MessageEvent<{ pcm: ArrayBuffer; rms: number }>) => {
      this.onLevel(e.data.rms);
      if (ws.readyState === WebSocket.OPEN) ws.send(e.data.pcm);
    };
  }

  stop(): void {
    this.stopped = true;
    this.ws?.close();
    this.stream?.getTracks().forEach((t) => t.stop());
    void this.ctx?.close();
    this.ws = null;
    this.stream = null;
    this.ctx = null;
    this.onLevel(0);
  }
}
