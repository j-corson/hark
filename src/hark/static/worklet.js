// Runs on the audio thread: downmix to mono, resample to 16 kHz, post ~100 ms Int16 chunks.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;        // fractional read position within the current input block
    this.acc = 0;        // running sum for the current output sample
    this.n = 0;          // inputs summed into acc
    this.out = new Int16Array(1600);
    this.k = 0;
    this.peak = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const ch = input.length, len = input[0].length;
    for (let i = 0; i < len; i++) {
      let v = 0;
      for (let c = 0; c < ch; c++) v += input[c][i];
      v /= ch;
      this.acc += v; this.n++;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.acc / this.n));
        this.acc = 0; this.n = 0;
        const a = Math.abs(s); if (a > this.peak) this.peak = a;
        this.out[this.k++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.k === this.out.length) {
          this.port.postMessage({ pcm: this.out.buffer, peak: this.peak }, [this.out.buffer]);
          this.out = new Int16Array(1600); this.k = 0; this.peak = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("hark-capture", Capture);
