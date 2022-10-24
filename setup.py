#!/usr/bin/env python3

from setuptools import setup

setup(
    name='aiolifx_effects',
    packages=['aiolifx_effects'],
    version='0.3.0',
    install_requires=['aiolifx>=0.8.6'],
    description='aiolifx light effects',
    author='Anders Melchiorsen',
    author_email='amelchio@nogoto.net',
    url='https://github.com/amelchio/aiolifx_effects',
    license='MIT',
    keywords=['aiolifx,lifx'],
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3 :: Only",
    ],
)
