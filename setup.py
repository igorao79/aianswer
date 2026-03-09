from setuptools import setup, find_packages

setup(
    name="AIAnswer",
    version="1.0.0",
    description="Screenshot-to-AI answer tool using Groq Llama 4 Scout vision",
    author="igorao79",
    py_modules=["main"],
    python_requires=">=3.9",
    install_requires=[
        "PyQt5>=5.15.0",
        "groq>=0.4.0",
        "keyboard>=0.13.5",
        "mss>=9.0.0",
        "Pillow>=10.0.0",
    ],
    entry_points={
        "console_scripts": [
            "aianswer=main:main",
        ],
    },
)
