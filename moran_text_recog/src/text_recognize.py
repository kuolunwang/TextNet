#!/usr/bin/env python3

import numpy as np
import cv2
import roslib
import rospy
import tf
import struct
import math
import time
import gdown
import tf.transformations as tfm
from sensor_msgs.msg import Image
from sensor_msgs.msg import CameraInfo, CompressedImage
from geometry_msgs.msg import PoseArray, PoseStamped
from geometry_msgs.msg import Pose
from visualization_msgs.msg import Marker, MarkerArray
import rospkg
from cv_bridge import CvBridge, CvBridgeError
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import os 
import message_filters
from text_msgs.msg import text_detection_msg, text_detection_array, int_arr
from text_msgs.srv import *

from PIL import Image as Im
import tools.utils as utils
import tools.dataset as dataset
from models.moran import MORAN
from collections import OrderedDict
from std_msgs.msg import String
class text_recognize(object):
    def __init__(self):
        r = rospkg.RosPack()
        self.path = r.get_path('moran_text_recog')
        self.prob_threshold = 0.90
        self.cv_bridge = CvBridge()
        self.commodity_list = []
        self.read_commodity(r.get_path('text_msgs') + "/config/commodity_list.txt")
        self.alphabet = '0:1:2:3:4:5:6:7:8:9:a:b:c:d:e:f:g:h:i:j:k:l:m:n:o:p:q:r:s:t:u:v:w:x:y:z:$' 

        self.br = tf.TransformBroadcaster()
        self.listener = tf.TransformListener()

        self.means = (0.485, 0.456, 0.406)
        self.stds = (0.229, 0.224, 0.225)
        self.bbox_thres = 1500

        self.color_map = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(255,255,255)] # 0 90 180 270 noise

        self.objects = []
        self.is_compressed = False

        self.cuda_use = torch.cuda.is_available()

        if self.cuda_use:
            cuda_flag = True
            self.network = MORAN(1, len(self.alphabet.split(':')), 256, 32, 100, BidirDecoder=True, CUDA=cuda_flag)
            self.network = self.network.cuda()
        else:
            self.network = MORAN(1, len(self.alphabet.split(':')), 256, 32, 100, BidirDecoder=True, inputDataType='torch.FloatTensor', CUDA=cuda_flag)

        model_path = self.__download_model(os.path.join(self.path, "weights/"))

        print("Moran Model Parameters number: " + str(self.count_parameters(self.network)))
        if self.cuda_use:
            state_dict = torch.load(model_path)
        else:
            state_dict = torch.load(model_path, map_location='cpu')
        MORAN_state_dict_rename = OrderedDict()
        for k, v in state_dict.items():
            name = k.replace("module.", "") # remove `module.`
            MORAN_state_dict_rename[name] = v
        self.network.load_state_dict(MORAN_state_dict_rename)
        self.converter = utils.strLabelConverterForAttention(self.alphabet, ':')
        self.transformer = dataset.resizeNormalize((100, 32))

        for p in self.network.parameters():
            p.requires_grad = False
        self.network.eval()

        #### Publisher
        self.speech_pub = rospy.Publisher("speech_case", String, queue_size = 1)
        self.image_pub = rospy.Publisher("~predict_img", Image, queue_size = 1)
        self.mask = rospy.Publisher("~mask", Image, queue_size = 1)
        self.img_bbox_pub = rospy.Publisher("~predict_bbox", Image, queue_size = 1)
        self.obj_pose_pub = rospy.Publisher("/object_pose", Pose, queue_size = 1)
        #### Service
        self.predict_ser = rospy.Service("~text_recognize_server", text_recognize_srv, self.srv_callback)

        image_sub1 = rospy.Subscriber('/text_detection_array', text_detection_array, self.callback, queue_size = 1)
        ### msg filter 
        # image_sub = message_filters.Subscriber('/camera/color/image_raw', Image)
        # depth_sub = message_filters.Subscriber('/camera/aligned_depth_to_color/image_raw', Image)
        # ts = message_filters.TimeSynchronizer([image_sub, depth_sub], 10)
        # ts.registerCallback(self.callback)
        print("============ Ready ============")

    def __download_model(self, path):
        model_url = 'https://drive.google.com/uc?export=download&id=1I_eSx0KIHMun881MMMc7AJ5-f483FHGP'
        model_name = 'moran'

        if not os.path.exists(os.path.join(path, model_name + '.pth')):
            gdown.download(model_url, output=os.path.join(path, model_name + '.pth'), quiet=False)
    
        print("Finished downloading model.")
        return os.path.join(path, model_name + '.pth')


    def read_commodity(self, path):

        for line in open(path, "r"):
            line = line.rstrip('\n')
            self.commodity_list.append(line)
        print("Node (text_recognize): Finish reading list")

    def count_parameters(self, model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    def callback(self, msg):
        try:
            if self.is_compressed:
                np_arr = np.fromstring(msg.image, np.uint8)
                cv_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            else:
                cv_image = self.cv_bridge.imgmsg_to_cv2(msg.image, "bgr8")
        except CvBridgeError as e:
            print(e)
        
        predict_img, mask = self.predict(msg, cv_image)
        img_bbox = cv_image.copy()

        try:
            self.image_pub.publish(self.cv_bridge.cv2_to_imgmsg(predict_img, "bgr8"))
            self.img_bbox_pub.publish(self.cv_bridge.cv2_to_imgmsg(img_bbox, "bgr8"))
            self.mask.publish(self.cv_bridge.cv2_to_imgmsg(mask, "8UC1"))
        except CvBridgeError as e:
            print(e)

    def srv_callback(self, req):
        resp = text_recognize_srvResponse()
        try:
            if self.is_compressed:
                np_arr = np.fromstring(req.data.image, np.uint8)
                cv_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            else:
                cv_image = self.cv_bridge.imgmsg_to_cv2(req.data.image, "bgr8")
        except CvBridgeError as e:
            resp.state = e
            print(e)
        
        predict_img, mask = self.predict(req.data, cv_image, req.direct)
        img_bbox = cv_image.copy()

        try:
            self.image_pub.publish(self.cv_bridge.cv2_to_imgmsg(predict_img, "bgr8"))
            self.img_bbox_pub.publish(self.cv_bridge.cv2_to_imgmsg(img_bbox, "bgr8"))
            resp.mask = self.cv_bridge.cv2_to_imgmsg(mask, "8UC1")
            self.mask.publish(self.cv_bridge.cv2_to_imgmsg(mask, "8UC1"))
        except CvBridgeError as e:
            resp.state = e
            print(e)

        return resp

    def predict(self, msg, img, rot=0):
        # # Preprocessing
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        (rows, cols, channels) = img.shape
        mask = np.zeros([rows, cols], dtype = np.uint8)

        # calculate real world point
        depth_msg = rospy.wait_for_message("/camera/aligned_depth_to_color/image_raw", Image)
        

        for text_bb in msg.text_array:
            if (text_bb.box.ymax - text_bb.box.ymin) * (text_bb.box.xmax - text_bb.box.xmin) < self.bbox_thres:
                continue
            start = time.time()
            image = gray[text_bb.box.ymin:text_bb.box.ymax, text_bb.box.xmin:text_bb.box.xmax]

            image = Im.fromarray(image) 
            image = self.transformer(image)

            if self.cuda_use:
                image = image.cuda()
            image = image.view(1, *image.size())
            image = Variable(image)
            text = torch.LongTensor(1 * 5)
            length = torch.IntTensor(1)
            text = Variable(text)
            length = Variable(length)

            max_iter = 20
            t, l = self.converter.encode('0'*max_iter)
            utils.loadData(text, t)
            utils.loadData(length, l)
            output = self.network(image, length, text, text, test=True, debug=True)

            preds, preds_reverse = output[0]
            demo = output[1]

            _, preds = preds.max(1)
            _, preds_reverse = preds_reverse.max(1)

            sim_preds = self.converter.decode(preds.data, length.data)
            sim_preds = sim_preds.strip().split('$')[0]
            sim_preds_reverse = self.converter.decode(preds_reverse.data, length.data)
            sim_preds_reverse = sim_preds_reverse.strip().split('$')[0]

            # print('\nResult:\n' + 'Left to Right: ' + sim_preds + '\nRight to Left: ' + sim_preds_reverse + '\n\n')
            print("Text Recognize Time : {}".format(time.time() - start))

            _cont = []
            for p in text_bb.contour:
                point = []
                point.append(p.point[0])
                point.append(p.point[1])
                _cont.append(point)
            _cont = np.array(_cont, np.int32)
            if sim_preds in self.commodity_list:
                cv2.rectangle(img, (text_bb.box.xmin, text_bb.box.ymin),(text_bb.box.xmax, text_bb.box.ymax), self.color_map[rot], 3)
                cv2.putText(img, sim_preds, (text_bb.box.xmin, text_bb.box.ymin), 0, 1, (0, 255, 255),3)
                pix = self.commodity_list.index(sim_preds) + rot*len(self.commodity_list)
                if pix in np.unique(mask):
                    cv2.fillConvexPoly(mask, _cont, pix + 4*len(self.commodity_list))
                else:
                    cv2.fillConvexPoly(mask, _cont, pix)
            else:
                correct, conf, _bool = self.conf_of_word(sim_preds)

                # print conf
                if _bool:
                    cv2.putText(img, correct + "{:.2f}".format(conf), (text_bb.box.xmin, text_bb.box.ymin), 0, 1, (0, 255, 255),3)
                    cv2.rectangle(img, (text_bb.box.xmin, text_bb.box.ymin),(text_bb.box.xmax, text_bb.box.ymax), (255, 255, 255), 2)
                    pix = self.commodity_list.index(correct) + rot*len(self.commodity_list)
                    if pix in np.unique(mask):
                        cv2.fillConvexPoly(mask, _cont, pix + 4*len(self.commodity_list))
                    else:
                        cv2.fillConvexPoly(mask, _cont, pix)
                # else:
                #     cv2.putText(img, sim_preds, (text_bb.box.xmin, text_bb.box.ymin), 0, 1, (0, 0, 0),3)
                #     cv2.rectangle(img, (text_bb.box.xmin, text_bb.box.ymin),(text_bb.box.xmax, text_bb.box.ymax), (0, 0, 0), 2)                    
            
            point = [(text_bb.box.xmin+text_bb.box.xmax)/2,(text_bb.box.ymin+text_bb.box.ymax)/2]
            self.Finddepth(depth_msg, point)
            position = self.transform_pose_to_base_link(self.real_world_point,(0,0,0,1))
            self.br.sendTransform(self.real_world_point,position,rospy.Time.now(),"object","camera_color_optical_frame")
            
            obj_pose = Pose()
            obj_pose.position.x = self.real_world_point[0]
            obj_pose.position.y = self.real_world_point[1]
            obj_pose.position.z = self.real_world_point[2]
            obj_pose.orientation.x = position[0]
            obj_pose.orientation.y = position[1]
            obj_pose.orientation.z = position[2]
            obj_pose.orientation.w = position[3]
            
            self.obj_pose_pub.publish(obj_pose)

            print(sim_preds)
            abc = String()
            abc.data = sim_preds
            self.speech_pub.publish(abc)

        return img, mask


    def transform_pose_to_base_link(self,t,q):


        eu = tfm.euler_from_quaternion(q)
        tf_cam_col_opt_fram = tfm.compose_matrix(t,eu)

        trans, quat = self.listener.lookupTransform('camera_color_optical_frame','camera_link',rospy.Time(0))
        euler = tfm.euler_from_quaternion(quat)
        tf = tfm.compose_matrix(trans,euler)

        t_pose = np.dot(tf, tf_cam_col_opt_fram)


        return tuple(quat)

    def Finddepth(self, depth_data, point):
        xp, yp = point[0], point[1]
        # Get the camera calibration parameter for the rectified image
        msg = rospy.wait_for_message('/camera/color/camera_info', CameraInfo)
        #     [fx'  0  cx' Tx]
        #P = [ 0  fy' cy' Ty]
        #     [ 0   0   1   0]
        fx = msg.P[0]
        fy = msg.P[5]
        cx = msg.P[2]
        cy = msg.P[6]
        try:
            cv_depthimage = self.cv_bridge.imgmsg_to_cv2(depth_data, "32FC1")
            cv_depthimage2 = np.array(cv_depthimage, dtype=np.float32)
        except CvBridgeError as e:
            print(e)
        if not math.isnan(cv_depthimage2[int(yp)][int(xp)]) :
            zc = cv_depthimage2[int(yp)][int(xp)]
            self.real_world_point = self.getXYZ(xp, yp, zc, fx, fy, cx, cy)
        # return getXYZ(xp, yp, zc, fx, fy, cx, cy)
        
    def getXYZ(self, xp, yp, zc, fx,fy,cx,cy):
        #### Definition:
        # cx, cy : image center(pixel)fd
        # fx, fy : focal length
        # xp, yp: index of the depth image
        # zc: depth
        inv_fx = 1.0/fx
        inv_fy = 1.0/fy
        x = (xp-cx) *  zc * inv_fx / 1000
        y = (yp-cy) *  zc * inv_fy / 1000
        z = zc / 1000
        return (x,y,z)

    def conf_of_word(self, target):
        ### Edit distance
        # print target

        _recheck = False
        total = np.zeros(len(self.commodity_list))
        for i in range(1, len(self.commodity_list)):
            size_x = len(self.commodity_list[i]) + 1
            size_y = len(target) + 1
            matrix = np.zeros ((size_x, size_y))
            for x in range(size_x):
                matrix [x, 0] = x
            for y in range(size_y):
                matrix [0, y] = y

            for x in range(1, size_x):
                for y in range(1, size_y):
                    if self.commodity_list[i][x-1] == target[y-1]:
                        matrix [x,y] = min(
                            matrix[x-1, y] + 1,
                            matrix[x-1, y-1],
                            matrix[x, y-1] + 1
                        )
                    else:
                        matrix [x,y] = min(
                            matrix[x-1,y] + 1,
                            matrix[x-1,y-1] + 1,
                            matrix[x,y-1] + 1
                        )
            # print (matrix)
            total[i] = (size_x - matrix[size_x-1, size_y-1]) / float(size_x)
            
            if self.commodity_list[i] == "kleenex" and 0.3 < total[i] < 0.77:
                _list = ["kloonex", "kloonox","kleeper", "killer", "kleem",  "kleers", "kluting", "klates",\
                    "kleams", "kreamer", "klea", "kleas", "kletter","keenier","vooney", "wooner", "whonex"]
                _recheck = True
            elif self.commodity_list[i] == "andes" and 0.3 < total[i] < 0.77:
                _list = ["anders", "findes","windes"]  # "andor", 
                _recheck = True
            elif self.commodity_list[i] == "vanish" and 0.3 < total[i] < 0.77:
                _list = ["varish"]
                _recheck = True
            # elif self.commodity_list[i] == "crayola" and 0.3 < total[i] < 0.77:
            #     _list = ["casions"]
            #     _recheck = True
            if _recheck == True:
                
                for _str in _list:
                    size_x = len(_str) + 1
                    size_y = len(target) + 1
                    matrix = np.zeros ((size_x, size_y))
                    for x in range(size_x):
                        matrix [x, 0] = x
                    for y in range(size_y):
                        matrix [0, y] = y

                    for x in range(1, size_x):
                        for y in range(1, size_y):
                            if _str[x-1] == target[y-1]:
                                matrix [x,y] = min(
                                    matrix[x-1, y] + 1,
                                    matrix[x-1, y-1],
                                    matrix[x, y-1] + 1
                                )
                            else:
                                matrix [x,y] = min(
                                    matrix[x-1,y] + 1,
                                    matrix[x-1,y-1] + 1,
                                    matrix[x,y-1] + 1
                                )
                    score_temp = (size_x - matrix[size_x-1, size_y-1]) / float(size_x)
                    if total[i] < score_temp:
                        total[i] = score_temp
                if 0.77 > total[i] > 0.68:
                    total[i] = 0.77
                _recheck = False

        return self.commodity_list[np.argmax(total)], np.max(total), np.max(total) >= 0.77   ## 0.66        

    def onShutdown(self):
        rospy.loginfo("Shutdown.")    
    

if __name__ == '__main__': 
    rospy.init_node('text_recognize',anonymous=False)
    text_recognize = text_recognize()
    rospy.on_shutdown(text_recognize.onShutdown)
    rospy.spin()
