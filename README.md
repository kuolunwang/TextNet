# TextNet

This is a ros package for TextNet, you need to clone this repo for your workspace and re-catkin make, then you can type below command to use it.
**The alogrithm only run in GPU computer!

## How to Use TextNet

```
     cd [your workspace]/src && git clone git@github.com:kuolunwang/TextNet.git
     cd [your workspace] && catkin_make
```

## Start detection by TextNet

Launch this file and open Rviz to see predicted result and mask. **Please make sure you open camera before launch predict file, in this case, we use D435 for example.**
```
    roslaunch realsense2_camera rs_rgbd.launch
    roslaunch textsnake text_detection.launch
```
## Start recognition by TextNet

```
    roslaunch moran_text_recog text_recognize.launch
```

Then, you will see prediction and mask images on Rviz, you can also edit [config file](moran_text_recog/config/commodity_list.txt) to determine what name can be detected.