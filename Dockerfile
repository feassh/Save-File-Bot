FROM python:3.13

# 安装 aria2 和 OpenCV 所需依赖
RUN apt-get update && \
    apt-get install -y \
    aria2 \
    libgl1 \
    ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件到镜像中
COPY requirements.txt /app/

# 安装 Python 依赖
RUN pip3 install --no-cache-dir -r requirements.txt

# 复制应用程序代码到镜像中
COPY . /app

# 运行容器时执行的命令
CMD ["python3", "main.py"]