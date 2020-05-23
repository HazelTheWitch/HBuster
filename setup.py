from setuptools import setup

setup(
    name='hbuster',
    version='1.0',
    py_modules=['hbuster'],
    install_requires=[
        'Click',
        'aiohttp',
    ],
    entry_points='''
        [console_scripts]
        hbuster=hbuster:hbuster
    ''',
)
