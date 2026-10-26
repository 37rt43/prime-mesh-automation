"""
PyPrimeMesh 最小示例：连接本机 Ansys Prime Server（2024R1），
可选导入 CAD 并用 Lucid 高层接口划分网格、导出。

用法：
1. 仅验证连接（启动服务、打印版本后退出）：
   python main.py

2. 导入 CAD 并自动划分网格（STEP/SCDOC/PMDB 等）：
   python main.py --cad part.step --out part.msh
   可选尺寸：
   python main.py --cad part.step --min-size 0.5 --max-size 5 --out part.msh

注意：本机为 Ansys 2024R1，客户端固定 0.5.x，虚拟环境用 Python 3.11。
"""

import argparse

import ansys.meshing.prime as prime


def launch_prime():
    """Central secure Prime launcher shared by CLI workflows."""
    return prime.launch_prime()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--cad", help="要导入的 CAD 文件路径（STEP/SCDOC/PMDB 等）")
    p.add_argument("--out", default="meshed.msh", help="网格输出文件名")
    p.add_argument("--min-size", type=float, default=1.0, help="最小网格尺寸")
    p.add_argument("--max-size", type=float, default=10.0, help="最大网格尺寸")
    return p.parse_args()


def main():
    args = parse_args()

    # launch_prime() 默认走安全连接，本机自动启动 Prime Server
    with launch_prime() as prime_client:
        model = prime_client.model
        print("PyPrimeMesh 客户端版本：", prime.__version__)
        print("Prime Server 已连接。")


        if not args.cad:
            # 没有 CAD，仅验证连接
            print("未提供 CAD 文件，连接验证通过，退出。")
            return

        # Lucid 高层接口：读入 -> 面网格 -> 体网格 -> 写出
        mesh_util = prime.lucid.Mesh(model=model)
        mesh_util.read(file_name=args.cad)

        mesh_util.surface_mesh(
            min_size=args.min_size,
            max_size=args.max_size,
        )
        mesh_util.volume_mesh(
            volume_fill_type=prime.VolumeFillType.POLY,
        )

        mesh_util.write(file_name=args.out)
        print("网格已导出：", args.out)


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
