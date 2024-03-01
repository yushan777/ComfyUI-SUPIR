import os
import torch
from torch.nn import functional as F
from contextlib import nullcontext
from omegaconf import OmegaConf
import comfy.utils
import comfy.model_management as mm
import folder_paths
from nodes import ImageScaleBy
import torch.cuda
from .sgm.util import instantiate_from_config
from .SUPIR.util import convert_dtype, load_state_dict

script_directory = os.path.dirname(os.path.abspath(__file__))


class SUPIR_Upscale:
    def __init__(self):
        self.current_sdxl_model = None
        self.current_supir_model = None
        self.current_diffusion_dtype = None
        self.current_encoder_dtype = None
        self.tiled_vae_state = None

    upscale_methods = ["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "supir_model": (folder_paths.get_filename_list("checkpoints"),),
            "sdxl_model": (folder_paths.get_filename_list("checkpoints"),),
            "image": ("IMAGE",),
            "seed": ("INT", {"default": 123, "min": 0, "max": 0xffffffffffffffff, "step": 1}),
            "resize_method": (s.upscale_methods, {"default": "lanczos"}),
            "scale_by": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 20.0, "step": 0.01}),
            "steps": ("INT", {"default": 45, "min": 3, "max": 4096, "step": 1}),
            "restoration_scale": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 6.0, "step": 1.0}),
            "cfg_scale": ("FLOAT", {"default": 7.5, "min": 0, "max": 20, "step": 0.01}),
            "a_prompt": ("STRING", {"multiline": True, "default": "high quality, detailed", }),
            "n_prompt": ("STRING", {"multiline": True, "default": "bad quality, blurry, messy", }),
            "s_churn": ("INT", {"default": 5, "min": 0, "max": 40, "step": 1}),
            "s_noise": ("FLOAT", {"default": 1.003, "min": 1.0, "max": 1.1, "step": 0.001}),
            "control_scale": ("FLOAT", {"default": 1.0, "min": 0, "max": 1, "step": 0.05}),
            "cfg_scale_start": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 9.0, "step": 0.05}),
            "control_scale_start": ("FLOAT", {"default": 0.0, "min": 0, "max": 1.0, "step": 0.05}),
            "color_fix_type": (
                [
                    'None',
                    'AdaIn',
                    'Wavelet',
                ], {
                    "default": 'None'
                }),
            "keep_model_loaded": ("BOOLEAN", {"default": True}),
            "use_tiled_vae": ("BOOLEAN", {"default": True}),
            "encoder_tile_size_pixels": ("INT", {"default": 512, "min": 64, "max": 8192, "step": 64}),
            "decoder_tile_size_latent": ("INT", {"default": 64, "min": 32, "max": 8192, "step": 64}),
        },
            "optional": {
                "captions": ("STRING", {"forceInput": True, "multiline": False, "default": "", }),
                "diffusion_dtype": (
                    [
                        'fp16',
                        'bf16',
                        'fp32',
                        'auto'
                    ], {
                        "default": 'auto'
                    }),
                "encoder_dtype": (
                    [
                        'bf16',
                        'fp32',
                        'auto'
                    ], {
                        "default": 'auto'
                    }),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 128, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("upscaled_image",)
    FUNCTION = "process"

    CATEGORY = "SUPIR"

    def process(self, steps, image, color_fix_type, seed, scale_by, cfg_scale, resize_method, s_churn, s_noise,
                encoder_tile_size_pixels, decoder_tile_size_latent,
                control_scale, cfg_scale_start, control_scale_start, restoration_scale, keep_model_loaded,
                a_prompt, n_prompt, sdxl_model, supir_model, use_tiled_vae, captions="", diffusion_dtype="auto",
                encoder_dtype="auto", batch_size=1):

        device = mm.get_torch_device()
        image = image.to(device)

        SUPIR_MODEL_PATH = folder_paths.get_full_path("checkpoints", supir_model)
        SDXL_MODEL_PATH = folder_paths.get_full_path("checkpoints", sdxl_model)

        config_path = os.path.join(script_directory, "options/SUPIR_v0.yaml")

        if diffusion_dtype == 'auto':
            try:
                if mm.should_use_bf16():
                    print("Diffusion using bf16")
                    dtype = torch.bfloat16
                    model_dtype = 'bf16'
                elif mm.should_use_fp16():
                    print("Diffusion using using fp16")
                    dtype = torch.float16
                    model_dtype = 'fp16'
                else:
                    print("Diffusion using using fp32")
                    dtype = torch.float32
                    model_dtype = 'fp32'
            except:
                raise AttributeError("ComfyUI too old, can't autodecet properly. Set your dtypes manually.")
        else:
            print(f"Diffusion using using {diffusion_dtype}")
            dtype = convert_dtype(diffusion_dtype)
            model_dtype = diffusion_dtype

        if encoder_dtype == 'auto':
            try:
                if mm.should_use_bf16():
                    print("Encoder using bf16")
                    vae_dtype = 'bf16'
                else:
                    print("Encoder using using fp32")
                    vae_dtype = 'fp32'
            except:
                raise AttributeError("ComfyUI too old, can't autodetect properly. Set your dtypes manually.")
        else:
            vae_dtype = encoder_dtype
            print(f"Encoder using using {vae_dtype}")

        if not hasattr(self, "model") or self.model is None or self.current_sdxl_model != sdxl_model or self.current_diffusion_dtype != diffusion_dtype or self.current_encoder_dtype != encoder_dtype or self.tiled_vae_state != use_tiled_vae or self.current_supir_model != supir_model:
            self.model = None
            mm.soft_empty_cache()
            self.current_diffusion_dtype = diffusion_dtype
            self.current_encoder_dtype = encoder_dtype
            self.current_sdxl_model = sdxl_model
            self.current_supir_model = supir_model
            config = OmegaConf.load(config_path)
            config.model.params.ae_dtype = vae_dtype
            config.model.params.diffusion_dtype = model_dtype
            self.model = instantiate_from_config(config.model).cpu()
            try:
                print(f'Attempting to load SUPIR model: [{SUPIR_MODEL_PATH}]')
                supir_state_dict = load_state_dict(SUPIR_MODEL_PATH)
            except:
                raise Exception("Failed to load SUPIR model")
            try:
                print(f"Attempting to load SDXL model: [{SDXL_MODEL_PATH}]")
                sdxl_state_dict = load_state_dict(SDXL_MODEL_PATH)
            except:
                raise Exception("Failed to load SDXL model")
            self.model.load_state_dict(supir_state_dict, strict=False)
            self.model.load_state_dict(sdxl_state_dict, strict=False)

            del supir_state_dict, sdxl_state_dict
            mm.soft_empty_cache()

            try:
                # to dtype first then to device to reduce memory usage
                self.model.to(dtype)
                self.model.to(device)
            except Exception as e:
                print("Failed to move model to device")
                print(e)
                import gc
                # unload everything and give up
                self.model = None
                del self.model
                gc.collect()
                mm.soft_empty_cache()

            if use_tiled_vae:
                self.tiled_vae_state = True
                self.model.init_tile_vae(encoder_tile_size=encoder_tile_size_pixels,
                                         decoder_tile_size=decoder_tile_size_latent)
            else:
                self.tiled_vae_state = False

        image, = ImageScaleBy.upscale(self, image, resize_method, scale_by)
        B, H, W, C = image.shape
        new_height = H // 64 * 64
        new_width = W // 64 * 64
        image = image.permute(0, 3, 1, 2).contiguous()
        resized_image = F.interpolate(image, size=(new_height, new_width), mode='bicubic', align_corners=False)
        resized_typed_images = resized_image.to(dtype)
        del resized_image
        mm.soft_empty_cache()

        captions_list = []
        captions_list.append(captions)
        print(captions_list)

        use_linear_CFG = cfg_scale_start > 0
        use_linear_control_scale = control_scale_start > 0
        out = []
        pbar = comfy.utils.ProgressBar(B)

        batched_images = [resized_typed_images[i:i + batch_size] for i in
                          range(0, len(resized_typed_images), batch_size)]
        captions_list = captions_list * resized_typed_images.shape[0]
        batched_captions = [captions_list[i:i + batch_size] for i in range(0, len(captions_list), batch_size)]

        mm.soft_empty_cache()
        i = 1
        for imgs, caps in zip(batched_images, batched_captions):
            try:
                samples = self.model.batchify_sample(imgs, caps, num_steps=steps,
                                                     restoration_scale=restoration_scale, s_churn=s_churn,
                                                     s_noise=s_noise, cfg_scale=cfg_scale, control_scale=control_scale,
                                                     seed=seed,
                                                     num_samples=1, p_p=a_prompt, n_p=n_prompt,
                                                     color_fix_type=color_fix_type,
                                                     use_linear_CFG=use_linear_CFG,
                                                     use_linear_control_scale=use_linear_control_scale,
                                                     cfg_scale_start=cfg_scale_start,
                                                     control_scale_start=control_scale_start)
            except torch.cuda.OutOfMemoryError as e:
                mm.free_memory(mm.get_total_memory(mm.get_torch_device()), mm.get_torch_device())
                self.model = None
                mm.soft_empty_cache()
                print("its likely that too large of a batch size for SUPIR was used,"
                      " and it has devoured all of the memory it had reserved, you will need to restart comfyui")
                raise e
            except Exception as e:
                self.model = None
                mm.soft_empty_cache()
                mm.free_memory(mm.get_total_memory(mm.get_torch_device()), mm.get_torch_device())
                print("its likely that too large of a batch size for SUPIR was used,"
                      " and it has devoured all of the memory it had reserved, you will need to restart comfyui")
                raise e

            out.append(samples.squeeze(0).cpu())
            print("Sampled ", i * len(imgs), " out of ", B)
            i = i + 1
            pbar.update(1)
        if not keep_model_loaded:
            self.model = None
            mm.soft_empty_cache()

        if len(out[0].shape) == 4:
            out_stacked = torch.cat(out, dim=0).cpu().to(torch.float32).permute(0, 2, 3, 1)
        else:
            out_stacked = torch.stack(out, dim=0).cpu().to(torch.float32).permute(0, 2, 3, 1)

        return (out_stacked,)


NODE_CLASS_MAPPINGS = {
    "SUPIR_Upscale": SUPIR_Upscale,
    "SUPIR_Upscale": SUPIR_Upscale
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SUPIR_Upscale": "SUPIR_Upscale",
    "SUPIR_Upscale": "SUPIR_Upscale"
}
