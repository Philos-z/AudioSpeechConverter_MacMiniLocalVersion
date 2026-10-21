"""Opt-in static gain at BigVGAN conv_post, BEFORE its internal hard clamp.

Version 1 wrapped BigVGAN.forward after the model's own final bound; that only
provided downstream PCM headroom and could not recover or prevent internal
saturation. Version 2 supports only explicit use_tanh_at_final=False and wraps
conv_post.forward, whose result is next bounded by BigVGAN's final torch.clamp.
No compressor, time edits, resampling or vendor-file patch is used.
"""
import math

MODE='bigvgan_conv_post_static_gain_before_internal_clamp_v2'


def protect_float_waveform(waveform,ceiling=.95):
    """Proportional gain only; the caller must supply the pre-bound float tensor."""
    import torch
    if not isinstance(waveform,torch.Tensor) or not waveform.is_floating_point():
        raise TypeError('Peak guard requires a floating Torch waveform')
    if not waveform.numel() or not bool(torch.isfinite(waveform).all().item()):
        raise ValueError('Peak guard refuses empty/non-finite vocoder output')
    if not 0<ceiling<1:raise ValueError('Peak ceiling must be between zero and one')
    peak=float(waveform.detach().abs().amax().item());gain=1.
    if peak>ceiling:
        safe_ceiling=ceiling*(1.-2.*float(torch.finfo(waveform.dtype).eps))
        gain=safe_ceiling/peak;protected=waveform*gain
    else:protected=waveform
    after=float(protected.detach().abs().amax().item())
    if after>ceiling:raise ValueError('Peak guard numeric ceiling invariant failed')
    record={'mode':MODE,'input_peak':peak,'ceiling':ceiling,'static_gain':gain,
        'gain_db':20.*math.log10(gain),'protected_peak':after,'applied':gain<1.,
        'shape':list(waveform.shape),'dtype':str(waveform.dtype),'device':str(waveform.device),
        'compressor_used':False,'time_or_pitch_changed':False,
        'quality_judgement':'Not assessed; preventing internal amplitude clipping does not establish timbre or naturalness.'}
    return protected,record


class WholeUtterancePreclipGuard:
    """Guard exactly one conv_post output per full utterance, not output chunks."""
    def __init__(self,module,ceiling=.95):
        if getattr(module,'use_tanh_at_final',None) is not False:
            raise ValueError('Preclip guard v2 supports only explicit BigVGAN use_tanh_at_final=False; tanh/unknown branch refused')
        target=getattr(module,'conv_post',None)
        if target is None or not callable(getattr(target,'forward',None)):
            raise ValueError('BigVGAN conv_post.forward is required for a genuine pre-internal-clamp guard')
        self.module=module;self.target=target;self.ceiling=ceiling
        self.original_forward=target.forward;self.active_id=None;self.records=[];self.installed=False
    def install(self):
        if self.installed:raise RuntimeError('Peak guard already installed')
        original=self.original_forward
        def guarded_conv_post(*args,**kwargs):
            if self.active_id is None:raise RuntimeError('Begin an utterance before conv_post protection')
            if self.records:raise RuntimeError('Multiple conv_post calls cannot guarantee one whole-utterance static gain')
            raw=original(*args,**kwargs)
            protected,record=protect_float_waveform(raw,self.ceiling)
            record.update(pre_internal_clamp_peak=record['input_peak'],
                raw_peak=record['input_peak'],
                point='BigVGAN conv_post.forward output BEFORE BigVGAN internal final hard clamp',
                model_bound_branch='explicit use_tanh_at_final=False',
                prevents_internal_clamp_for_this_tensor=True)
            self.records.append(record);return protected
        self.target.forward=guarded_conv_post;self.installed=True;return self
    def begin(self,identifier):
        if self.active_id is not None:raise RuntimeError('Previous protected utterance was not finished')
        self.active_id=identifier;self.records=[]
    def finish(self):
        if self.active_id is None or len(self.records)!=1:
            raise RuntimeError('Expected exactly one conv_post output for this whole utterance')
        result={'utterance_id':self.active_id,'conv_post_calls':1,**self.records[0]}
        self.active_id=None;return result
    def restore(self):
        self.target.forward=self.original_forward;self.installed=False;self.active_id=None
