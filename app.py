import os
import base64
import re
import sys

sys.path.append(os.path.abspath(os.path.join(__file__, '../')))
if 'OMP_NUM_THREADS' not in os.environ:
    os.environ['OMP_NUM_THREADS'] = '32'

import shutil
import torch
import json
import requests
import shutil
import threading
from PIL import Image
import time
torch.backends.cuda.matmul.allow_tf32 = True
import trimesh

import random
import time
import numpy as np
from video_render import render_video_from_obj

from pipeline.kiss3d_wrapper import init_wrapper_from_config, run_text_to_3d, run_image_to_3d, image2mesh_preprocess, image2mesh_main

import gradio as gr
is_running = False

KISS_3D_TEXT_FOLDER = "./outputs/text2"
KISS_3D_IMG_FOLDER = "./outputs/image2"


LOGO_PATH = "assets/logo.jpg" 
ARXIV_LINK = "https://arxiv.org/abs/example"
GITHUB_LINK = "https://github.com/example"

k3d_wrapper = init_wrapper_from_config('./pipeline/pipeline_config/default.yaml')


TEMP_MESH_ADDRESS=''

mesh_cache = None
preprocessed_input_image = None

def save_cached_mesh():
    global mesh_cache
    return mesh_cache

def save_py3dmesh_with_trimesh_fast(meshes, save_glb_path=TEMP_MESH_ADDRESS, apply_sRGB_to_LinearRGB=True):
    from pytorch3d.structures import Meshes
    import trimesh

    vertices = meshes.verts_packed().cpu().float().numpy()
    triangles = meshes.faces_packed().cpu().long().numpy()
    np_color = meshes.textures.verts_features_packed().cpu().float().numpy()
    if save_glb_path.endswith(".glb"):
        vertices[:, [0, 2]] = -vertices[:, [0, 2]]

    def srgb_to_linear(c_srgb):
        c_linear = np.where(c_srgb <= 0.04045, c_srgb / 12.92, ((c_srgb + 0.055) / 1.055) ** 2.4)
        return c_linear.clip(0, 1.)
    if apply_sRGB_to_LinearRGB:
        np_color = srgb_to_linear(np_color)
    assert vertices.shape[0] == np_color.shape[0]
    assert np_color.shape[1] == 3
    assert 0 <= np_color.min() and np_color.max() <= 1, f"min={np_color.min()}, max={np_color.max()}"
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, vertex_colors=np_color)
    mesh.remove_unreferenced_vertices()
    mesh.export(save_glb_path)
    if save_glb_path.endswith(".glb"):
        fix_vert_color_glb(save_glb_path)
    print(f"saving to {save_glb_path}")
    
def text_to_detailed(prompt, seed=None):
    print(f"Before text_to_detailed: {torch.cuda.memory_allocated() / 1024**3} GB")
    return k3d_wrapper.get_detailed_prompt(prompt, seed)

def text_to_image(prompt, seed=None, strength=1.0,lora_scale=1.0, num_inference_steps=30, redux_hparam=None, init_image=None, init_image_path=None, **kwargs):
    print(f"Before text_to_image: {torch.cuda.memory_allocated() / 1024**3} GB")
    k3d_wrapper.renew_uuid()
    init_image = None
    if init_image_path is not None:
        init_image = Image.open(init_image_path)
    result = k3d_wrapper.generate_3d_bundle_image_text( 
                                      prompt,
                                      image=init_image, 
                                      strength=strength,
                                      lora_scale=lora_scale,
                                      num_inference_steps=num_inference_steps,
                                      seed=int(seed) if seed is not None else None,
                                      redux_hparam=redux_hparam,
                                      save_intermediate_results=True,
                                      **kwargs)
    return result[-1]

def image2mesh_preprocess_(input_image_, seed, use_mv_rgb=True):
    global preprocessed_input_image

    seed = int(seed) if seed is not None else None

    k3d_wrapper.del_llm_model()
    
    input_image_save_path, reference_save_path, caption = image2mesh_preprocess(k3d_wrapper, input_image_, seed, use_mv_rgb)

    preprocessed_input_image = Image.open(input_image_save_path)
    return reference_save_path, caption

def image2mesh_main_(reference_3d_bundle_image, caption, seed, strength1=0.5, strength2=0.95, enable_redux=True, use_controlnet=True, if_video=True):
    global mesh_cache 
    seed = int(seed) if seed is not None else None


    k3d_wrapper.del_llm_model()

    input_image = preprocessed_input_image

    reference_3d_bundle_image = torch.tensor(reference_3d_bundle_image).permute(2,0,1)/255

    gen_save_path, recon_mesh_path = image2mesh_main(k3d_wrapper, input_image, reference_3d_bundle_image, caption=caption, seed=seed, strength1=strength1, strength2=strength2, enable_redux=enable_redux, use_controlnet=use_controlnet)
    mesh_cache = recon_mesh_path


    if if_video:
        video_path = recon_mesh_path.replace('.obj','.mp4').replace('.glb','.mp4')
        render_video_from_obj(recon_mesh_path.replace('.glb','.obj'), video_path)
        print(f"After bundle_image_to_mesh: {torch.cuda.memory_allocated() / 1024**3} GB")
        return gen_save_path, video_path, mesh_cache
    else:
        return gen_save_path, recon_mesh_path, mesh_cache

def bundle_image_to_mesh(
        gen_3d_bundle_image, 
        lrm_radius = 4.15,
        isomer_radius = 4.5,
        reconstruction_stage1_steps = 10,
        reconstruction_stage2_steps = 50,
         save_intermediate_results=True, 
        if_video=True
    ):
    global mesh_cache
    print(f"Before bundle_image_to_mesh: {torch.cuda.memory_allocated() / 1024**3} GB")

    k3d_wrapper.del_llm_model()

    print(f"Before bundle_image_to_mesh after deleting llm model: {torch.cuda.memory_allocated() / 1024**3} GB")

    gen_3d_bundle_image = torch.tensor(gen_3d_bundle_image).permute(2,0,1)/255
    
    recon_mesh_path = k3d_wrapper.reconstruct_3d_bundle_image(gen_3d_bundle_image, lrm_render_radius=lrm_radius, isomer_radius=isomer_radius, save_intermediate_results=save_intermediate_results, reconstruction_stage1_steps=int(reconstruction_stage1_steps), reconstruction_stage2_steps=int(reconstruction_stage2_steps))
    mesh_cache = recon_mesh_path
    
    if if_video:
        video_path = recon_mesh_path.replace('.obj','.mp4').replace('.glb','.mp4')
        render_video_from_obj(recon_mesh_path.replace('.glb','.obj'), video_path)
        print(f"After bundle_image_to_mesh: {torch.cuda.memory_allocated() / 1024**3} GB")
        return video_path, mesh_cache
    else:
        return recon_mesh_path, mesh_cache

_HEADER_=f"""
<img src="{LOGO_PATH}">
    <h2><b>Official 🤗 Gradio Demo</b></h2><h2>
    <b>Kiss3DGen: Repurposing Image Diffusion Models for 3D Asset Generation</b></a></h2>


[![arXiv](https://img.shields.io/badge/arXiv-Link-red)]({ARXIV_LINK})  [![GitHub](https://img.shields.io/badge/GitHub-Repo-blue)]({GITHUB_LINK})
"""

_CITE_ = r"""
<h2>If Kiss3DGen is helpful, please help to ⭐ the <a href='{""" + GITHUB_LINK + r"""}' target='_blank'>Github Repo</a>. Thanks!</h2>

📝 **Citation**

If you find our work useful for your research or applications, please cite using this bibtex:
```bibtex
@article{xxxx,
  title={xxxx},
  author={xxxx},
  journal={xxxx},
  year={xxxx}
}
```

📋 **License**

Apache-2.0 LICENSE. Please refer to the [LICENSE file](https://huggingface.co/spaces/TencentARC/InstantMesh/blob/main/LICENSE) for details.

📧 **Contact**

If you have any questions, feel free to open a discussion or contact us at <b>xxx@xxxx</b>.
"""

def image_to_base64(image_path):
    """Converts an image file to a base64-encoded string."""
    with open(image_path, "rb") as img_file:
        return base64.b64encode(img_file.read()).decode('utf-8')

def main():

    torch.set_grad_enabled(False)

    logo_base64 = image_to_base64(LOGO_PATH)
    with gr.Blocks(css="""
        body {
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 0;
        }
        #col-container { margin: 0px auto; max-width: 1200px; } 


        .gradio-container {
            max-width: 1200px;
            margin: auto;
            width: 100%;
        }
        #center-align-column {
            display: flex;
            justify-content: center;
            align-items: center;
        }
        #right-align-column {
            display: flex;
            justify-content: flex-end;
            align-items: center;
        }
        h1 {text-align: center;}
        h2 {text-align: center;}
        h3 {text-align: center;}
        p {text-align: center;}
        img {text-align: right;}
        .right {
        display: block;
        margin-left: auto;
        }
        .center {
        display: block;
        margin-left: auto;
        margin-right: auto;
        width: 50%;

        #content-container {
            max-width: 1200px;
            margin: 0 auto;
        }
        #example-container {
            max-width: 1200px;
            margin: 0 auto;
        }
    """,elem_id="col-container") as demo:
        with gr.Row(elem_id="content-container"):
            with gr.Column(scale=7, elem_id="center-align-column"):
                gr.Markdown(f"""
                ## Official 🤗 Gradio Demo
                # Kiss3DGen: Repurposing Image Diffusion Models for 3D Asset Generation""")
                gr.HTML(f"<img src='data:image/png;base64,{logo_base64}' alt='Logo' class='center' style='width:64px;height:64px;border:0;text-align:center;'>")

                gr.HTML(f"""
                <div style="display: flex; justify-content: center; align-items: center; gap: 10px;">
                    <a href="{ARXIV_LINK}" target="_blank">
                        <img src="https://img.shields.io/badge/arXiv-Link-red" alt="arXiv">
                    </a>
                    <a href="{GITHUB_LINK}" target="_blank">
                        <img src="https://img.shields.io/badge/GitHub-Repo-blue" alt="GitHub">
                    </a>
                </div>
                
                """)

        with gr.Tabs(selected='tab_text_to_3d', elem_id="content-container") as main_tabs:
            with gr.TabItem('Text-to-3D', id='tab_text_to_3d'):
                with gr.Row():
                    with gr.Column(scale=1):
                        prompt = gr.Textbox(value="", label="Input Prompt", lines=4)
                        seed1 = gr.Number(value=10, label="Seed")

                        with gr.Row(elem_id="example-container"):
                            gr.Examples(
                                examples=[
                                    ["A girl with pink hair"],
                                    ["A boy playing guitar"],


                                    ["A dog wearing a hat"],
                                    ["A boy playing basketball"],

                                ],
                                inputs=[prompt],
                                label="Example Prompts"
                            )
                        btn_text2detailed = gr.Button("Refine to detailed prompt")
                        detailed_prompt = gr.Textbox(value="", label="Detailed Prompt", placeholder="detailed prompt will be generated here base on your input prompt. You can also edit this prompt", lines=4, interactive=True)
                        btn_text2img = gr.Button("Generate Images")

                    with gr.Column(scale=1):
                        output_image1 = gr.Image(label="Generated image", interactive=False)
                        btn_gen_mesh = gr.Button("Generate Mesh")
                        output_video1 = gr.Video(label="Generated Video", interactive=False, loop=True, autoplay=True)
                        download_1 = gr.DownloadButton(label="Download mesh", interactive=False)
                        
            with gr.TabItem('Image-to-3D', id='tab_image_to_3d'):
                with gr.Row():
                    with gr.Column():
                        image = gr.Image(label="Input Image", type="pil")
                        
                        seed2 = gr.Number(value=10, label="Seed (0 for random)")

                        btn_img2mesh_preprocess = gr.Button("Preprocess Image")

                        image_caption = gr.Textbox(value="", label="Image Caption", placeholder="caption will be generated here base on your input image. You can also edit this caption", lines=4, interactive=True)
                        
                        with gr.Accordion(label="Extra Settings", open=False):
                            output_image2 = gr.Image(label="Generated image", interactive=False)
                            strength1 = gr.Slider(minimum=0, maximum=1.0, step=0.01, value=0.5, label="redux strength")
                            strength2 = gr.Slider(minimum=0, maximum=1.0, step=0.01, value=0.95, label="denoise strength")
                            enable_redux = gr.Checkbox(label="enable redux", value=True)
                            use_controlnet = gr.Checkbox(label="enable controlnet", value=True)

                        btn_img2mesh_main = gr.Button("Generate Mesh")

                    with gr.Column():

                        output_image3 = gr.Image(label="Final Bundle Image", interactive=False)
                        output_video2 = gr.Video(label="Generated Video", interactive=False, loop=True, autoplay=True)
                        download_2 = gr.DownloadButton(label="Download mesh", interactive=False)

        btn_img2mesh_preprocess.click(fn=image2mesh_preprocess_, inputs=[image, seed2], outputs=[output_image2, image_caption])

        btn_img2mesh_main.click(fn=image2mesh_main_, inputs=[output_image2, image_caption, seed2, strength1, strength2, enable_redux, use_controlnet], outputs=[output_image3, output_video2, download_2]).then(
            lambda: gr.Button(interactive=True),
            outputs=[download_2],
        )


        btn_text2detailed.click(fn=text_to_detailed, inputs=[prompt, seed1], outputs=detailed_prompt)
        btn_text2img.click(fn=text_to_image, inputs=[detailed_prompt, seed1], outputs=output_image1)
        btn_gen_mesh.click(fn=bundle_image_to_mesh, inputs=[output_image1,], outputs=[output_video1, download_1]).then(
            lambda: gr.Button(interactive=True),
            outputs=[download_1],
        )

        with gr.Row():
            pass
        with gr.Row():
            gr.Markdown(_CITE_)

    demo.launch(server_name="0.0.0.0", server_port=9239)


if __name__ == "__main__":
    main()
