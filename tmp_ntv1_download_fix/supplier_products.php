<?php if (!defined('BASEPATH')) exit('No direct script access allowed');

class Products extends CI_Controller
{
    /**
     * Variable for loading the config array into
     * @var array
     */
    public $info;
    public $view_data;
    public $tmp_upload_url;

	function __construct()
	{
		parent::__construct();
		//debug($this->config);exit;
		//$this->output->enable_profiler(TRUE);

		if(!$this->session->userdata('user_id')) redirect('/auth/login/');

        $this->config_vars = $this->config->item('market');
		//debug($this->session->all_userdata());

		//$this->load->library('esmplus_scrap');
		//Console::log($this->esmplus_scrap->config_vars);

		$this->load->library('esmplus_scrap');
		$this->load->library('elevenSt_scrap');
		$this->load->library('coupang_scrap');
		$this->load->library('storefarm_scrap');
		//$this->load->library('wemakeprice_scrap');
		$this->load->library('sinsang_scrap');

		$this->tmp_upload_url = $this->config->item('user_temp_image_dir').$this->session->userdata('user_id');
		if(!is_dir($this->tmp_upload_url)) mkdir($this->tmp_upload_url, 0755, TRUE);
		//debug($this->tmp_upload_url);exit;

		$this->info['created'] = date('Y-m-d H:i:s', time());

		$this->view_data = $this->session->all_userdata();
		$this->view_data['is_mobile'] = 'N';

        $this->load->library('user_agent');
        if ($this->agent->is_mobile()) $this->view_data['is_mobile'] = 'Y';
		//debug($this->view_data);

			// 상품코드 이미지 다운 권한 세션
			//$this->view_data['down_level'] = $this->session->userdata('down_level');
		}

		function _download_safe_file_part($name, $fallback)
		{
			$name = trim((string)$name);
			if($name === '') $name = $fallback;
			$name = str_replace(array("|","\\","/","?","*","<","\"",":",">"), "_", $name);
			$name = preg_replace('/\s+/', '_', $name);
			$name = trim($name, "._- \t\n\r\0\x0B");
			if($name === '') $name = $fallback;
			if(function_exists('mb_substr')) $name = mb_substr($name, 0, 80, 'UTF-8');
			else $name = substr($name, 0, 80);
			return $name;
		}

		function _download_unique_zip_filename($prefix, $display_name='')
		{
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$token = substr(md5(uniqid('', TRUE).mt_rand()), 0, 8);
			$name = $this->_download_safe_file_part($display_name, $prefix);
			return $prefix.'_'.$user_id.'_'.date('YmdHis').'_'.$token.'_'.$name.'.zip';
		}

	function _download_request_zip_filename($prefix, $default_file)
	{
		$file = $this->input->get('file');
		if(!$file) return $default_file;

			$file = basename($file);
			if(substr($file, -4) !== '.zip' || strpos($file, $prefix.'_') !== 0)
				alert_close('다운로드 파일이 유효하지 않습니다!');

		return $file;
	}

	function _goodscode_image_files($goods_code)
	{
		$img_dir = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$files = glob($img_dir . '*');
		$names = array();
		if ($files) {
			foreach ($files as $f) {
				if (is_file($f)) $names[] = basename($f);
			}
		}
		natsort($names);
		return array_values($names);
	}

	function _goodscode_web_path_if_exists($goods_code, $filename, $thumbnail = false)
	{
		if (!$goods_code || !$filename) return '';
		$base_dir = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$base_url = $this->config->item('user_goodscode_img_url') . $goods_code . '/';
		$rel = $thumbnail ? 'thumbnail/' . $filename : $filename;
		return is_file($base_dir . $rel) ? $base_url . $rel : '';
	}

	function _goodscode_list_image($goods_code)
	{
		$candidates = array(
			$goods_code . '_list1.jpg',
			$goods_code . '_list2.jpg',
			$goods_code . '_list3.jpg',
			$goods_code . '-s_1.jpg',
			$goods_code . '_01.jpg',
		);
		foreach ($candidates as $name) {
			$path = $this->_goodscode_web_path_if_exists($goods_code, $name, true);
			if ($path) return $path;
			$path = $this->_goodscode_web_path_if_exists($goods_code, $name, false);
			if ($path) return $path;
		}
		return '';
	}

		function index()
		{
	        // 베스트 상품(2017.11.10)
		$wishing = @$this->uri->segment(3);
		if($wishing == 'best') $this->view_data['wishing'] = $wishing;

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag3'] = link_tag('/assets/css/cropper.min.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		// 카카오스토리 APP Key 추가
		$query = $this->db->query("SELECT sns_key FROM user_sns WHERE user_id='{$this->view_data['user_id']}' AND sns_gb='A' AND activated='1'");
		$row = $query->row();
		$sns_key = $row->sns_key;
		if($sns_key)
			$this->view_data['sns_key'] = $sns_key;
		else
			$this->view_data['sns_key'] = 'eadf8be73fdee750e41725566405edbf'; // 디폴트 키(APP명 : 쇼핑몰)

		//debug($this->view_data);
		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_list.php', $this->view_data);
		$this->load->view('bottom');
	}

    // 상품 정보 수정
	function index_edit()
	{
        //debug($this->input->post());
		$oper = $this->input->post('oper');

		// if(isset($this->input->post('GoodsEtc42')))
			$GoodsEtc42 = $this->input->post('GoodsEtc42');			// 도매몰판매가 수정
		// if(isset($this->input->post('GoodsEtc6Sort')))
			$GoodsEtc6Sort = $this->input->post('GoodsEtc6Sort'); 	// 매입처별 New 상품(미니도매몰) 정렬 번호 반영(2021.01.22)

        $GoodsId = $this->input->post('GoodsId');

        if($oper != 'edit')
        {
            echo '{"error": "정상적인 접근이 아닙니다!"}';
            return;
        }

		if($GoodsEtc6Sort > -1)
		{
			// 상품 goods 테이블 수정
			$goods_data = array(
				'GoodsEtc6Sort'	=> $GoodsEtc6Sort
			);
		}

		if($GoodsEtc42 > 0)
		{
			// 상품 goods 테이블 수정
			$goods_data = array(
				'GoodsEtc42'	=> $GoodsEtc42
			);
		}

        //debug($goods_data);
        $where_array = array('id' => $GoodsId, 'GoodsEtc6' => $this->view_data['username']);
        $this->db->where($where_array);
        $this->db->update('goods', $goods_data);

        echo '{"error": ""}';
    }

	// 상품 조회
	function goods_master_ajax_list()
	{
        // 베스트상품(2017.11.10)
		$wishing = @$this->uri->segment(3);

		// thumbnail 생성
		$this->load->library('image_lib');

		$goods_array = $this->config->item('goods_array');

		//debug($_REQUEST);
		$user_id = $this->session->userdata('user_id');
		$username = $this->session->userdata('username');	// 매입처와 매칭(2017.06.16)
		$goods_cnt = $this->session->userdata('goods_cnt');	// 공급회원 상품목록수(2017.06.23)

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$where = "";

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;

				if($fieldName == 'GoodsName') $fieldData = strtoupper($fieldData);
				if($fieldName == 'GoodsCode') $fieldData = strtolower($fieldData);
                if($fieldName == 'GoodsName') {
                    $fieldName = 'GoodsEtc5';
                    //debug($fieldName);
				}

				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		$brand = " and GS.BrandName not in ('무드인더', '베러스윗') ";

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = 'GS.'.$_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "GS.created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 베스트 상품목록
		if($wishing == 'best')
		{
			$query = $this->db->query("
										SELECT count(GSB.id) AS cnt
											FROM
												goods_best	As GSB LEFT OUTER JOIN
												goods		AS GS ON GSB.goods_id = GS.id
											WHERE
												GSB.user_id = '{$user_id}'
												AND
												GSB.checking = 'Y'
												AND
												GoodsEtc6='{$username}'
												AND
												( GoodsEtc52='2' OR GoodsEtc52='3' OR GoodsEtc52='4' ) {$brand} {$where}
									");
		}
		else
		{     // 전체 상품목록
			$query = $this->db->query(" SELECT count(id) AS cnt FROM goods As GS WHERE GoodsEtc6='{$username}' AND ( GoodsEtc52='2' OR GoodsEtc52='3' OR GoodsEtc52='4' ) {$brand} {$where} ");
		}
		$row = $query->row();
		// debug($row);
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

        // 상품목록
		if($wishing == 'best')
		{
			$sql = "SELECT
						GSB.checking, GSB.created as best_created,
						GS.*,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9
						FROM
							goods_best	As GSB LEFT OUTER JOIN
							goods		AS GS ON GSB.goods_id = GS.id LEFT OUTER JOIN
							goods_image	As GSI ON GS.id = GSI.goods_id
						WHERE
							GSB.user_id = '{$user_id}'
							AND
							GSB.checking = 'Y'
							AND
							GoodsEtc6='{$username}'
							AND
							( GoodsEtc52='2' OR GoodsEtc52='3' OR GoodsEtc52='4' ) {$brand} {$where}
						ORDER BY
							$sidx $sord
						LIMIT
							$start , $limit
			";
		}
		else
		{
    		$sql = "SELECT
    					GS.*,
    					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9,
                        GSB.checking
    					FROM
    						goods		AS GS LEFT OUTER JOIN
    						goods_image	As GSI ON GS.id = GSI.goods_id LEFT OUTER JOIN
                            goods_best	As GSB ON GS.id = GSB.goods_id AND GSB.user_id = '{$user_id}'
    					WHERE
							GoodsEtc6='{$username}' AND ( GoodsEtc52='2' OR GoodsEtc52='3' OR GoodsEtc52='4' ) {$brand} {$where}
    					ORDER BY
    						GS.GoodsEtc6Sort DESC, $sidx $sord
    					LIMIT
    						$start , $limit
    		";
        }
		// debug($sql);
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
                if(!$row->checking) $row->checking = 'N';

				// 관리자 상품등록 시 고정(2017.03.03 추가)
				//if($row->InAdm == 'Y') $row->user_id = '5';

				$img_home = $this->config->config['user_temp_image_dir'].$row->user_id;	// 절대경로
				$img_path = '/data/files/goods/'.$row->user_id;								// 페이지경로
				//debug($img_home);

				//debug(count(explode('/', $row->GoodsImage)));
				if(count(explode('/', $row->GoodsImage)) > 1)
					$GoodsImage	= $row->GoodsImage;
				else
				{
						if($row->GoodsImage)
						{
							$GoodsImage	 = '/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->GoodsImage;
							if(!is_file($this->config->config['user_temp_image_dir'].$row->user_id.'/thumbnail/'.$row->GoodsImage))
							{
								$goods_code_image = $this->_goodscode_list_image($row->GoodsCode);
								if($goods_code_image) $GoodsImage = $goods_code_image;
							}
							//$GoodsImage1 = ($row->img1)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img1:'';
							//$GoodsImage2 = ($row->img2)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img2:'';
							//$GoodsImage3 = ($row->img3)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img3:'';
							//$GoodsImage4 = ($row->img4)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img4:'';
						}
						else $GoodsImage = $this->_goodscode_list_image($row->GoodsCode);

					for($j=0; $j<10; $j++)
					{
						$img_name = $row->{'img'.$j};
						if($img_name)
						{
							$config['source_image']	= $img_home.'/'.$img_name;
							$config['new_image']	= $img_home.'/thumbnail/';
							if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

							if(!is_file($config['new_image'].$img_name))
							{
								$config['image_library'] = 'gd2';
								$config['maintain_ratio'] = TRUE;
								$config['width']	= 100;
								$config['height']	= 100;
								//debug($config);
								$this->image_lib->initialize($config);
								$this->image_lib->resize();
							}
						}

						${'GoodsImage'.$j} = (is_file($config['new_image'].$img_name)) ? $img_path.'/thumbnail/'.$img_name : '';
					}
				}

				//if($row->SendCheck == 'Y') $SendCheck = '';

                // 공급회원 상품별 상세페이지 이동을 위해 반영(2021.05.26)
				$userid = '';
				if($row->GoodsEtc6)
				{
					$query_a = $this->db->query("SELECT userid FROM users WHERE username='{$row->GoodsEtc6}'");
					$row_a = $query_a->row();
					$userid = $row_a->userid;
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
                    $row->id,
                    $row->GdsMstId,
                    $row->GoodsEtc6Sort,
                    $GoodsImage,
                    '',
                    $row->GoodsEtc5,
                    $row->GoodsCode,
                    $row->activated,
                    $row->mall_activated,
                    $goods_array['52'][$row->GoodsEtc52],
                    $row->checking,
                    $row->GoodsEtc9,
                    ($row->GoodsEtc42 > 0)?$row->GoodsEtc42:$row->GoodsEtc9,	// 2020.05.13 GoodsEtc42 -> GoodsEtc9 변경
                    ($wishing == 'best')?$row->best_created:$row->created,
                    $row->re_created,
                    '',
                    $GoodsImage1,
                    $GoodsImage2,
                    $GoodsImage3,
                    $GoodsImage4,
                    $userid,
                );
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 조회
	function goods_ajax_list()
	{
		//debug($_GET);
		$GdsMstId = $this->uri->segment(3);
		$user_id = $this->session->userdata('user_id');

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$GdsMstId}' ORDER BY id ASC");

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsName,
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 조회
	function ajax_list()
	{
		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods WHERE user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->GoodsName,
													'MK-'.$row->GdsMstId.'#GK-'.$row->id,
													$row->GoodsName,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품수정 이미지 처리
	function image_process()
	{
		$this->load->helper('cf_purge');
		$output = array();
		if(@$this->uri->segment(3))
		{
			$gb = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			$goods_id = $this->input->post('id'); // 상품 일련번호
			$key = $this->input->post('key'); // 삭제할 이미지 필드명
			//$del_img_name = $this->input->post('name'); // 삭제할 이미지명

			// 상품이미지 삭제
			if($gb == 'd')
			{
				// 등록 상품 정보 확인
				$sql = "SELECT
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
							FROM goods_image As GSI
							WHERE GSI.goods_id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();
				//debug($row);

				// 삭제할 필드의 이미지명이 같은지 확인
				if($row->{$del_field} == $del_img_name)
				{
					$del_img_name_arr = explode('.', $del_img_name);
					$del_img_thumb_name = $del_img_name_arr[0].'_thumb.'.$del_img_name_arr[1];

					$del_files_url1 = $this->tmp_upload_url.'/'.$del_img_name;
					$del_files_url2 = $this->tmp_upload_url.'/'.$del_img_thumb_name;
					//debug($del_files_url1);
					//debug($del_files_url2);

					//delete_files($del_files_url1);
					if(file_exists($del_files_url1))
						$output['uploaded'] = 'OK';
					else
						$output['uploaded'] = 'ERROR';
				}
				else
					$output['uploaded'] = 'ERROR';

				/*
				$img_path = '/data/files/goods/'.$user_id.'/';
				$img_cnt = 0;
				$initial_preview = '';
				$initial_preview_config = '';
				for($i=0; $i<15; $i++)
				{
					$img_name = $row->{'img'.$i};

					if($key == $i)
					{
						$initial_preview .= '"<img style=\'height:160px\' src=\''.$img_name.'\'>",';
						$initial_preview_config .= '{"caption": "메인이미지", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'"},';
					}
					else
					{
						if($img_name)
						{
							//echo $img_name;
							$initial_preview .= '"<img style=\'height:160px\' src=\''.$img_name.'\'>",';
							$initial_preview_config .= '{"caption": "'.$img_name.'", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'", "extra": {"id":'.$goods_id.',"name":"'.$img_name.'"}},';
						}
						else
						{
							$initial_preview .= '"<img style=\'height:160px\' src=\'\'>",';
							$initial_preview_config .= '{"caption": "", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'"},';
						}
					}
				}
				//echo $initial_preview.'<br>';
				//echo $initial_preview_config;
				//$initial['append'] = false;
				//$initial_preview = stripslashes(json_encode($initial['initial_preview']));
				//$initial_preview_config = stripslashes(json_encode($initial['initial_preview_config']));
				//substr_replace( $initial_preview, "", -1 );

				echo '{"initialPreview": ['.substr_replace( $initial_preview, "", -1 ).'], "initialPreviewConfig": ['.substr_replace( $initial_preview_config, "", -1 ).'],"append":false}';
				//echo stripslashes(json_encode($initial));

				//$test = json_encode(['initialPreview' => [$initial_preview],'initialPreviewConfig' => [$initial_preview_config],'append' => false]);
				*/
				echo json_encode($output);
			}
			// 상품이미지 수정 임시저장
			if($gb == 'm')
			{
				//debug($_FILES);exit;
				//debug($this->tmp_upload_url);

				$config['upload_path'] = $this->tmp_upload_url;
				$config['allowed_types'] = 'gif|jpg|png';
				$config['overwrite']	= TRUE;
				//$this->load->library('upload', $config);

				if(empty($_FILES['GoodsImage']))
				{
					//echo json_encode(['error'=>'No files found for upload.']);
					// or you can throw an exception
					return; // terminate
				}

				$images = $_FILES['GoodsImage'];
				//debug($images);exit;
				$success = null;
				$paths = array();
				$filenames = $images['name'];

				// loop and process files
				$img_path = '/data/files/goods/'.$user_id.'/';
				for($i=0; $i < count($filenames); $i++)
				{
					//$ext = explode('.', basename($filenames[$i]));
					$target = $this->tmp_upload_url."/".$filenames[$i];
					if(move_uploaded_file($images['tmp_name'][$i], $target))
					{
						$success = true;
						$paths[] = $img_path.$filenames[$i];
					}
					else
					{
						$success = false;
						break;
					}
				}

				if ($success === true)
				{
					$output['uploaded'] = $paths;
				}
				elseif ($success === false)
				{
					$output['error'] = 'Error while uploading images. Contact the system administrator';
					// delete any uploaded files
					foreach ($paths as $file) {
						unlink($file);
					}
				}
				else {
					$output['error'] = 'No files were processed.';
				}

				echo json_encode($output);
			}

			// 상품이미지 등록/복사 임시저장
			if($gb == 'u')
			{
				//debug($_FILES);
				//debug($this->tmp_upload_url);

				$config['upload_path'] = $this->tmp_upload_url;
				$config['allowed_types'] = 'gif|jpg|png';
				$config['overwrite']	= TRUE;
				$this->load->library('upload', $config);

				$field_name = "GoodsImage";
				if(!$this->upload->do_upload($field_name))
				{
					$error = array('error' => $this->upload->display_errors());
					//debug($error);

					$output['uploaded'] = 'ERROR';
				}
				else
				{
					$data = array('upload_data' => $this->upload->data());

					$output['uploaded'] = 'OK';
				}

				if (!empty($data['GoodsCode'])) cf_purge_goods($data['GoodsCode']);
				echo json_encode($output);
			}
		}
	}

	// 단하루 카테고리 정보
	function get_category($code='', $url=true)
	{
		$rtn = true;

		if($url && @$this->uri->segment(3))
		{
			$rtn = false;
			$code = $this->uri->segment(3);
			if(@$this->uri->segment(4)) $code .= '|'.$this->uri->segment(4);
		}
		//debug('code : '.$code);

		if($code)
		{
			$code_arr = explode('|', $code);
			//debug($code_arr);

			if(count($code_arr) == 1)
			{
				//debug($code);
				$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
				if($query->num_rows() > 0)
				{
					$row = $query->row();
					//debug($row);

					$query = $this->db->query("SELECT id, MiddleCategory FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' group by MiddleCategory order by MiddleCode asc");
					$i = 0;
					foreach ($query->result() as $row)
					{
						if($row->MiddleCategory)
						{
							$data[$i]['Code'] = $row->id;
							$data[$i]['Name'] = $row->MiddleCategory;
							$i++;
						}
					}

					if(count($data) > 0)
						$data = json_encode($data);
					else
						$data = '[]';
				}
			}
			else
			{
				if($code_arr[1] == '1')
				{
					//debug($code);
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code_arr[0]}");
					if($query->num_rows() > 0)
					{
						$row = $query->row();
						//debug($row);

						$query = $this->db->query("SELECT id, MiddleCategory FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' group by MiddleCategory order by MiddleCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->MiddleCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->MiddleCategory;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				elseif($code_arr[1] == '2')
				{
					$code = $code_arr[0];
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
					if($query->num_rows() > 0)
					{
						$row = $query->row();

						$query = $this->db->query("SELECT id, SmallCategory, CategoryCode FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and MiddleCategory='{$row->MiddleCategory}' group by SmallCategory order by SmallCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->SmallCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->SmallCategory;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				else $data = '[]';
			}

		}
		else
		{
			$query = $this->db->query("SELECT id, LargeCategory FROM goods_cate WHERE market='H' group by LargeCategory order by LargeCode asc");
			if($query->num_rows() > 0)
			{
				$i = 0;
				foreach ($query->result() as $row)
				{
					$data[$i]['Code'] = $row->id;
					$data[$i]['Name'] = $row->LargeCategory;
					$i++;
				}
			}
		}

		//debug($data);

		if($rtn)
			return $data;
		else
			echo $data;
	}

	// 상품 이미지 확인
	function goods_image_check($data)
	{
		if(isset($data['DataRtn'])) $DataRtn = true;
		else $DataRtn = false;

		// 상품 이미지 확인 및 등록
		if($data['GoodsImageList'])
		{
			$GoodsImageArr = explode('||', $data['GoodsImageList']);
			//debug($GoodsImageArr);
			$tmp_file_arr = get_filenames($this->tmp_upload_url); // 임시 이미지 배열
			//debug($tmp_file_arr);

			foreach($GoodsImageArr AS $k => $v)
			{
				if($v)
				{
					// 임시경로에 이미지 확인, 있으면 등록경로에 업로드
					if( in_array($v, $tmp_file_arr) )
					{
						// 메인이미지만 썸네일 저장
						//if($k == 0)
						//{
							$config['source_image']	= $this->tmp_upload_url.'/'.$v;
							$config['new_image']	= $this->tmp_upload_url.'/thumbnail/';
							if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

							if(!is_file($config['new_image'].$v))
							{
								// thumbnail 생성
								$this->load->library('image_lib');
								$config['image_library'] = 'gd2';
								$config['maintain_ratio'] = TRUE;
								$config['width']	= 100;
								$config['height']	= 100;
								//debug($config);
								$this->image_lib->initialize($config);
								$this->image_lib->resize();
							}
						//}
					}
					else
					{
						if($DataRtn) return '{"info":{"success":false,"error":{"kind":"img","key":"'.$k.'","msg":"임시이미지에 이미지가 없습니다."}}}';
						echo '{"info":{"success":false,"text":"이미지 등록 오류(2)입니다."}}';
						exit;
					}
				}
			}

			if($DataRtn) return $GoodsImageArr;
			echo '{"info":{"success":true}}';
		}
		else
		{
			if($DataRtn) return '{"info":{"success":false,"error":{"kind":"img","key":"A","msg":"이미지 데이타가 없습니다."}}}';
			echo '{"info":{"success":false,"text":"이미지 등록 오류(1)입니다."}}';
			exit;
		}

	}

    // 베스트 상품 선택 담기 / 빼기
	function goods_best_plus_minus_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$gb || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		if($gb == 'P') $checking = 'Y';	// 담기
		if($gb == 'M') $checking = 'N';	// 빼기

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 찜상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_best WHERE user_id='{$user_id}' AND goods_id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				$goods_wish_rows = $query1->row();
				//debug($goods_wish_rows);

				// 아니면 등록
				if($query1->num_rows() < 1)
				{
					$insert_data = array(
						'user_id'	=> $user_id,
						'goods_id'	=> $goods_id,
						'checking'	=> $checking,
						'check_ip'	=> $this->input->ip_address(),
						'created'	=> $this->info['created']
					);
					$this->db->insert('goods_best', $insert_data);

					$success_cnt++; // 성공 시 카운트
				}
				// 있으면 업데이트
				else
				{
					$update_data = array(
						'checking' => $checking,
						'check_ip' => $this->input->ip_address()
					);
					//debug($update_data);
					$this->db->where('id', $goods_wish_rows->id);
					$this->db->update('goods_best', $update_data);

					$success_cnt++; // 성공 시 카운트
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

    // 상품 미니몰 선택 노출 / 해제
	function goods_mall_view_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$gb || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		if($gb == 'Y') $checking = 'Y';	// 노출
		if($gb == 'N') $checking = 'N';	// 해제

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods WHERE user_id=5 AND id='{$goods_id}' ";
				//debug($sql1);
				$query1 = $this->db->query($sql1);
				$goods_rows = $query1->row();
				//debug($goods_rows);

				// 있으면 업데이트
				if($query1->num_rows() > 0)
				{
					$update_data = array(
						'mall_activated' => $checking
					);
					//debug($update_data);
					$this->db->where('id', $goods_rows->id);
					$this->db->update('goods', $update_data);

					$success_cnt++; // 성공 카운트
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 이미지 압축 다운
	function zip_select()
	{
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.Description, GSD.NoticeItemCodes,
								GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row)
						{
							$img_home = $this->config->config['user_temp_image_dir'].$row->user_id;		// 절대경로
							$img_path = '/data/files/goods/'.$row->user_id;								// 페이지경로
							//debug($img_home);

							for($j=0; $j<15; $j++)
							{
								$img_name = $row->{'img'.$j};
								if($img_name)
								{
									$zip_path = $img_home.'/'.$img_name;
									//debug($zip_path);
									if(is_file($img_home.'/'.$img_name))
									{
										$this->zip->read_file($zip_path);
										//debug($zip_path);
									}
								}
							}

							$this->zip->archive($this->config->config['user_temp_image_dir'].'/myarchive.zip');
							//$this->zip->download('myarchive.zip');

							$update_data = array(
								'SendCheck' => $SendCheck
							);
							//$this->db->where('id', $row2->id);
							//$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다.","url":"/data/files/goods/myarchive.zip"}}';
	}

	// 마켓별 카테고리
	function category($scrap, $market, $code=NULL, $json=FALSE)
	{
		//debug($scrap, 1);
		$json_cate1 = ARRAY();

		if($scrap)
		{
			$market_name = $market.'_name';
			$market_cate1 = $market.'_cate1';

			$this->config_vars['path_cookie'][$market] = $scrap->config_vars['path_cookie'][$market];
			$this->view_data[$market_name] = $scrap->config_vars['open_market2'][$market];

			// 11번가
			if($market == "C")
			{
				$this->view_data['C_cate1'] = $scrap->get_site_cate('C', $code);
				//debug($this->view_data['C_cate1']);
				$this->view_data['C_cate1'] = substr_replace( $this->view_data['C_cate1'], "", 0, 1 );
				$this->view_data['C_cate1'] = substr_replace( $this->view_data['C_cate1'], "", -1 );

				if(!$code)
				{
					$this->view_data['C_cate1'] = str_replace("'conditionList':'[]'", '"conditionList":[]', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("'lCategoryList':'", '"lCategoryList":', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("','mCategoryList':'[]'", ',"mCategoryList":[]', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("'sCategoryList':'[]'", '"sCategoryList":[]', $this->view_data['C_cate1'] );
					$C_cate1 = json_decode($this->view_data['C_cate1'], 1);
					$this->view_data['C_cate1'] = $C_cate1['lCategoryList'];
				}
				else
				{
					$C_cate1 = json_decode($this->view_data['C_cate1'], 1);
					//debug($C_cate1);
					$this->view_data['C_cate1'] = $C_cate1;
				}

				if($json)
					$this->view_data['C_cate1'] = json_encode($this->view_data['C_cate1']);
			}
			// 신상마켓
			else if($market == "G")
			{
				$this->view_data['G_cate1'] = $scrap->get_site_cate('G', $code);
				//debug($this->view_data['G_cate1']);
			}
			else
			{
				$json_cate1 = json_decode($scrap->get_site_cate($market, $code), 1);

				// ESMPLUS
				if(isset($json_cate1['Result']))
					$this->view_data[$market_cate1] = $json_cate1['Result'];
				// 스토어팜
				else if(isset($json_cate1['htReturnValue']))
					$this->view_data[$market_cate1] = $json_cate1['htReturnValue'];
				// 위메프
				else if(isset($json_cate1['category_data']))
					$this->view_data[$market_cate1] = $json_cate1['category_data'];
				// 쿠팡
				else
					$this->view_data[$market_cate1] = $json_cate1;

				if($json)
					$this->view_data[$market_cate1] = json_encode($this->view_data[$market_cate1]);
			}
		}
	}

	// 마켓별 카테고리(중분류 부터 사용) GET 방식 호출 함수
	function getcategory()
	{
		if(@$this->uri->segment(3) && @$this->uri->segment(4))
		{
			$market = $this->uri->segment(3);
			$code = $this->uri->segment(4);
			if(@$this->uri->segment(5)) $code .= '|'.$this->uri->segment(5);
			//debug($market);debug($code);

			$market_cate1 = $market.'_cate1';

			switch($market)
			{
				// 옥션
				case "A":
					$this->esmplus_scrap->login('A');
					if($this->esmplus_scrap->config_vars['path_cookie']['A']['login']) $this->category($this->esmplus_scrap, 'A', $code, TRUE);
					break;
				// 지마켓
				case "B":
					$this->esmplus_scrap->login('B');
					if($this->esmplus_scrap->config_vars['path_cookie']['B']['login']) $this->category($this->esmplus_scrap, 'B', $code, TRUE);
					break;
				// 11번가
				case "C":
					$this->elevenst_scrap->login('C');
					//debug($this->elevenst_scrap->config_vars);
					if($this->elevenst_scrap->config_vars['path_cookie']['C']['login']) $this->category($this->elevenst_scrap, 'C', $code, TRUE);
					break;
				// 쿠팡
				case "D":
					$this->coupang_scrap->login('D');
					//debug($this->coupang_scrap->config_vars);
					if($this->coupang_scrap->config_vars['path_cookie']['D']['login']) $this->category($this->coupang_scrap, 'D', $code, TRUE);
					break;
				// 스토어팜
				case "E":
					$this->storefarm_scrap->login('E');
					//debug($this->coupang_scrap->config_vars);
					if($this->storefarm_scrap->config_vars['path_cookie']['E']['login']) $this->category($this->storefarm_scrap, 'E', $code, TRUE);
					break;
				// 위메프
				case "F":
					$this->wemakeprice_scrap->login('F');
					//debug($this->coupang_scrap->config_vars);
					if($this->wemakeprice_scrap->config_vars['path_cookie']['F']['login']) $this->category($this->wemakeprice_scrap, 'F', $code, TRUE);
					break;
				// 신성마켓
				case "G":
					$this->sinsang_scrap->login('G');
					//debug($this->sinsang_scrap->config_vars);
					if($this->sinsang_scrap->config_vars['path_cookie']['G']['login']) $this->category($this->sinsang_scrap, 'G', $code, TRUE);
					break;
			}

			echo $this->view_data[$market_cate1];

		}
		else
			echo 'false';
	}

	// 상품코드 목록
	function goods_code()
	{
		$this->load->helper('directory');

		$goods_img_dir = $this->config->item('user_goodscode_img_dir');

		$map = directory_map($goods_img_dir);
		//debug($map);
		$this->view_data['my_list'] = $map;
		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_code.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품코드 이미지 압축다운
	// JSZip용 이미지 URL 목록 반환
	function goods_zip_urls()
	{
		if($this->view_data['down_level'] < 1) {
			echo json_encode(['success' => false, 'msg' => '다운로드 권한이 없습니다.']);
			return;
		}

		$goods_code = $this->input->get('code');
		if(!$goods_code) {
			echo json_encode(['success' => false, 'msg' => '잘못된 접근입니다.']);
			return;
		}

		$img_dir  = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$cdn_base = 'https://img.newtalk.kr/data/files/goods/goodscode/img/' . $goods_code . '/';

			$names = $this->_goodscode_image_files($goods_code);
			$urls  = [];
			foreach ($names as $name) {
				$urls[] = [
					'url' => $cdn_base . rawurlencode($name),
					'fallback_url' => '/products/goods_zip_file?code=' . rawurlencode($goods_code) . '&file=' . rawurlencode($name),
					'zip_path' => $goods_code . '/' . $name
				];
			}

		$sql = "SELECT GS.*, GSD.* FROM goods AS GS
				LEFT OUTER JOIN goods_detail AS GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'";
		$goods_info = $this->db->query($sql)->row();
		$txt = '';
		$goods_name = $goods_code;
		if ($goods_info && $goods_info->GoodsName) {
			if ($goods_info->GoodsEtc5) $goods_info->GoodsName = $goods_info->GoodsEtc5;
			$goods_name = $goods_info->GoodsName;
			$txt .= '[ ' . $goods_info->GoodsName . ' ]' . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= $goods_info->Description . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= 'Product Info_' . PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= ' - FABRIC(소재) : ' . $goods_info->GoodsEtc16 . PHP_EOL.PHP_EOL;
			$txt .= ' - COLOR(색상) : ' . $goods_info->OptionColor . PHP_EOL.PHP_EOL;
			$txt .= ' - WASHING(세탁방법) : ' . $goods_info->GoodsEtc18 . PHP_EOL.PHP_EOL;
			$txt .= ' - Weight(무게:g) : ' . $goods_info->GoodsEtc17 . PHP_EOL.PHP_EOL;
			$txt .= ' - SIZE(사이즈) : ' . $goods_info->OptionSize . PHP_EOL.PHP_EOL;
			$txt .= ' - SizeSpec(상세사이즈) : ' . $goods_info->GoodsEtc14 . PHP_EOL.PHP_EOL;
		}

		header('Content-Type: application/json; charset=utf-8');
		echo json_encode([
			'success'    => true,
			'goods_code' => $goods_code,
				'goods_name' => $goods_name,
				'zip_name'   => $goods_name . '.zip',
				'partial_zip_name' => $goods_name . '_partial_missing.zip',
				'expected_count' => count($urls),
				'txt'        => $txt,
				'txt_name'   => $goods_code . '.txt',
				'images'     => $urls,
			], JSON_UNESCAPED_UNICODE);
		}

		function goods_zip_download_log()
		{
			$goods_code = $this->input->post('code');
			$expected = intval($this->input->post('expected'));
			$success = intval($this->input->post('success'));
			$failed = $this->input->post('failed');
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$log = array(
				'type' => 'jszip_partial_download',
				'user_id' => $user_id,
				'goods_code' => $goods_code,
				'expected' => $expected,
				'success' => $success,
				'failed' => json_decode($failed, true),
				'ip' => $this->input->ip_address(),
				'ua' => substr((string)$this->input->user_agent(), 0, 200),
			);
			log_message('error', '[goods_zip_download] ' . json_encode($log, JSON_UNESCAPED_UNICODE));
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(array('success' => true));
		}

		function goods_zip_file()
		{
			if($this->view_data['down_level'] < 1) {
				show_404();
				return;
			}

			$goods_code = preg_replace('/[^a-zA-Z0-9_-]/', '', (string)$this->input->get('code'));
			$file = basename((string)$this->input->get('file'));
			if(!$goods_code || !$file || !in_array($file, $this->_goodscode_image_files($goods_code))) {
				show_404();
				return;
			}

			$file_path = $this->config->item('user_goodscode_img_dir') . $goods_code . '/' . $file;
			if(!is_file($file_path)) {
				show_404();
				return;
			}

			$mime = function_exists('mime_content_type') ? mime_content_type($file_path) : 'application/octet-stream';
			header('Content-Type: ' . ($mime ? $mime : 'application/octet-stream'));
			header('Content-Length: ' . filesize($file_path));
			header('Cache-Control: private, no-store, max-age=0');
			header('X-NewTalk-Download-Fallback: 1');
			readfile($file_path);
		}

	function goods_code_zip_down()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->model('goods_m');

		if($this->view_data['down_level'] < 1)
			alert('다운로드 권한이 없습니다!');

		$goods_id = $this->input->get('id');
		$goods_code = $this->input->get('code');
		$gb = $this->input->get('gb') != null ? $this->input->get('gb') : "";

		if(!$goods_id && !$goods_code)
			alert_close('정상적인 접근이 아닙니다!');

		$this->load->library('zip'); // Zip 압축 클래스 초기화
		$this->load->library('excel');

		$excel_header_data = $this->config->item('excel_header_data');
		$excel_data = $this->config->item('excel_data');
		$excel_width_data = $this->config->item('excel_width_data');
		//debug($excel_width_data);

		// $goods_code = $this->uri->segment(3);

		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';

			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.id AS goods_pk,
						GS.*,
						GSD.*,
						GSI.*
					FROM
						goods AS		GS LEFT OUTER JOIN
						goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
						goods_image		As GSI ON GS.id = GSI.goods_id
					WHERE GS.GoodsCode='{$goods_code}'
		";
			$query = $this->db->query($sql);
			$goods_info = $query->row();
			if(!$goods_info)
				alert_close('해당 상품 정보가 존재하지 않습니다!');
			$download_goods_id = (isset($goods_info->goods_pk) && $goods_info->goods_pk) ? $goods_info->goods_pk : $goods_id;
			//debug($sql);

		// 상품분류 (대>중>소>세)
		$goods_cate = '';
		$goods_cate1 = $goods_info->Category1;
		$goods_cate2_arr = explode('|', $goods_info->Category2);
		$goods_cate3_arr = explode('|', $goods_info->Category3);
		if($goods_cate1)
		{
			$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
			if($query->num_rows() > 0) $cate1 = $query->row();

			if($cate1->LargeCode)
			{
				$goods_cate .= $cate1->LargeCode;

				if($goods_cate2_arr[0])
				{
					$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
					if($query->num_rows() > 0) $cate2 = $query->row();

					if($cate2->MiddleCode)
					{
						$goods_cate .= '>'.$cate2->MiddleCode;

						if($goods_cate3_arr[0])
						{
							$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
							if($query->num_rows() > 0) $cate3 = $query->row();

							if($cate3->SmallCode)
								$goods_cate .= '>'.$cate3->SmallCode;
						}
					}
				}
			}
		}
		//debug($goods_cate);

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($excel_header_data AS $k => $v)
		{
			$headers[] = $v;
		}
		//debug($headers);

		// 엑셀 데이타
		$excel_data['A'] = $goods_info->GoodsName;	// 상품명[필수]
		$excel_data['B'] = $goods_info->GoodsEtc7;	// 약어
		$excel_data['C'] = $goods_info->GoodsEtc5;	// 모델명
		$excel_data['F'] = $goods_info->GoodsCode;	// 자체상품코드
		$excel_data['G'] = $goods_info->GoodsEtc24;	// 사이트검색어
		$excel_data['H'] = $goods_info->GoodsEtc48;	// 상품구분[필수]
		$excel_data['I'] = $goods_cate;					// 상품분류 (대>중>소>세)
		$excel_data['M'] = $goods_info->GoodsEtc21;	// 원산지(제조국)[필수]
		$excel_data['O'] = $goods_info->GoodsEtc20;	// 제조일
		$excel_data['P'] = $goods_info->GoodsEtc51;	// 시즌
		$excel_data['R'] = $goods_info->GoodsEtc52;	// 상품상태[필수]
		$excel_data['T'] = $goods_info->GoodsEtc53;	// 세금구분[필수]
		$excel_data['U'] = $goods_info->GoodsEtc54;	// 배송비구분[필수]
		$excel_data['X'] = $goods_info->GoodsEtc9;	// 원가
		$excel_data['Y'] = $goods_info->GoodsPrice;	// 판매가[필수]
		$excel_data['Z'] = $goods_info->GoodsEtc32;	// TAG가[필수]
		$excel_data['AB'] = $goods_info->OptionColor;	// 옵션상세명칭(1)
		$excel_data['AD'] = $goods_info->OptionSize;	// 옵션상세명칭(2)
		if($goods_info->GoodsImage)
		{
			$excel_data['AE'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->GoodsImage;	// 대표이미지[필수]
			$excel_data['AF'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->GoodsImage;	// 종합몰JPG이미지
		}
		if($goods_info->img1)
			$excel_data['AG'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1;	// 부가이미지2
		if($goods_info->img2)
			$excel_data['AH'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img2;	// 부가이미지3
		if($goods_info->img3)
			$excel_data['AI'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img3;	// 부가이미지4
		if($goods_info->img4)
			$excel_data['AJ'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img4;	// 부가이미지5
		if($goods_info->img5)
			$excel_data['AK'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img5;	// 부가이미지6
		if($goods_info->img6)
			$excel_data['AL'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img6;	// 부가이미지7
		if($goods_info->img7)
			$excel_data['AM'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img7;	// 부가이미지8
		if($goods_info->img8)
			$excel_data['AN'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img8;	// 부가이미지9
		if($goods_info->img9)
			$excel_data['AO'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img9;	// 부가이미지10
		$excel_data['AP'] = $goods_info->Description;	// 상품상세설명[필수]
		$excel_data['AS'] = $goods_info->GoodsEtc56;	// 재고관리사용여부
		$excel_data['AT'] = $goods_info->GoodsEtc10;	// 원가2
		if($goods_info->img10)
			$excel_data['AU'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img10;	// 부가이미지11
		if($goods_info->img11)
			$excel_data['AV'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img11;	// 부가이미지12
		if($goods_info->img12)
			$excel_data['AY'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img12;	// 부가이미지14
		if($goods_info->img13)
			$excel_data['AZ'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img13;	// 부가이미지15
		if($goods_info->img14)
			$excel_data['BA'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img14;	// 부가이미지16
		$excel_data['BJ'] = $goods_info->GoodsEtc4;	// 영문상품명
		$excel_data['BK'] = $goods_info->GoodsEtc8;	// 출력상품명
		$excel_data['BL'] = $goods_info->GoodsEtc27;	// 추가 상품상세설명_1(선택입력)
		$excel_data['BM'] = $goods_info->GoodsEtc28;	// 추가 상품상세설명_2(선택입력)
		$excel_data['BN'] = $goods_info->GoodsEtc29;	// 추가 상품상세설명_3(선택입력)
		$excel_data['BO'] = $goods_info->GoodsEtc57;	// 속성분류코드[필수]

		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v;
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		$rows = array(
			$real_excel_data,
		);
		//debug($rows);
		$data = array_merge(array($headers), $rows);
		//debug($data);

		// 스타일 지정
		$widths = array();
		foreach($excel_width_data AS $k => $v)
		{
			$widths[] = $v;
		}
		//debug(count($widths));
		$header_bgcolor = 'FFABCDEF';

		// 엑셀 생성
		$last_char = column_char( count($headers) - 1 );
		//debug($last_char);

		$excel = new PHPExcel();
		//$excel->setActiveSheetIndex(0)->getStyle( "A1:${last_char}1" )->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB($header_bgcolor);
		//$excel->setActiveSheetIndex(0)->getStyle( "A:$last_char" )->getAlignment()->setVertical(PHPExcel_Style_Alignment::VERTICAL_CENTER)->setWrapText(true);
		foreach($widths as $i => $w) $excel->setActiveSheetIndex(0)->getColumnDimension( column_char($i) )->setWidth($w);
		//$excel->getActiveSheet()->fromArray($data, NULL, 'A1');
		//$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel2007');
		//$writer->save($this->config->item('user_temp_image_dir').$goods_code.'.xlsx');

		$col1 = 0;
		foreach ($headers as $title)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col1, 1, $title);
			$col1++;
		}
		$col2 = 0;
		foreach($real_excel_data as $record)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col2, 2, $record);
			$col2++;
		}

		// Add some data
		/*
		$excel->setActiveSheetIndex(0)
			->setCellValue("A1", $headers[0])->setCellValue("A2", $real_excel_data[0])
			->setCellValue("B1", $headers[1])->setCellValue("B2", $real_excel_data[1])
			->setCellValue("C1", $headers[2])->setCellValue("C2", $real_excel_data[2])
			->setCellValue("D1", $headers[3])->setCellValue("D2", $real_excel_data[3])
			->setCellValue("E1", $headers[4])->setCellValue("E2", $real_excel_data[4])
			->setCellValue("F1", $headers[5])->setCellValue("F2", $real_excel_data[5])
			->setCellValue("G1", $headers[6])->setCellValue("G2", $real_excel_data[6])
		;
		*/

		$excel->getActiveSheet()->setTitle('단하루 상품');
		$excel->setActiveSheetIndex(0);
		$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel5');

		//$writer->save($this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_code.'.xls');
		//$this->zip->read_file($this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_code.'.xls');

		$data_txt = '';
		if($goods_info->GoodsName)
		{
			// 공급회원은 상품명을 사입명으로 변경(2020.07.22)
			if($goods_info->GoodsEtc5) $goods_info->GoodsName = $goods_info->GoodsEtc5;

			$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
			$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
			$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
			$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
			$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조사 : 단하루 협력업체'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조일 : 배송기준 3개월이내'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조국 : 대한민국'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 품질보증기준 : 소비자보호 법에 준함'.PHP_EOL.PHP_EOL;
			$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

			$data = array(
                $goods_code.'.txt' => $data_txt
            );
			//debug($data);
			$this->zip->add_data($data);
		}

		$this->zip->read_dir($goods_img_dir, FALSE);

			$this->goods_m->down_status($this->view_data['user_id'], $download_goods_id, $goods_code);

			if($gb == 'ajax')
	        {
				$down_file = $this->_download_unique_zip_filename('pick_goods_img', $goods_info->GoodsEtc5 ? $goods_info->GoodsEtc5 : $goods_code);
				if($this->zip->archive($this->config->config['user_pick_image_dir'].$down_file))
	            {
				    echo json_encode(array('info' => array('success' => true, 'downfile' => $down_file)), JSON_UNESCAPED_UNICODE);
				    exit;
	            }
            else {
			    echo '{"info":{"success":false,"text":"다운로드 오류입니다."}}';
			    exit;
            }
        }
        else{
		$this->zip->download(urlencode($goods_info->GoodsEtc5).'.zip'); // urlencode('단하루상품이미지텍스트파일').'('.date('YmdHis', time()).').zip'

        }

		// alert_close('다운로드가 완료되었습니다!');
		alert('다운로드가 완료되었습니다!');
	}

	// 상품코드 이미지 압축다운
	function goods_code_select_zip_down()
	{
		ini_set('memory_limit','-1');

		if($this->view_data['down_level'] < 1)
		{
			echo '{"info":{"success":false,"text":"다운로드 권한이 없습니다!"}}';
			exit;
		}

		$goodsCode = $this->input->post('goodsCode');
		//debug($goodsCode);

		if(!$goodsCode)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$setting_cnt = count($goodsCode); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		$this->zip->clear_data();

		foreach($goodsCode AS $k => $goods_code)
		{
			if($goods_code)
			{
				$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';

				// 등록 상품 정보 확인
				$sql = "SELECT
							GSD.Description
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id
							WHERE GS.GoodsCode='{$goods_code}'
				";
				//debug($sql);

				$query = $this->db->query($sql);

				if($query->num_rows() > 0)
				{
					foreach($query->result() as $row)
					{
						if($row->Description)
						{
							$data = array(
								$goods_code.'.txt' => $row->Description
							);
							$this->zip->add_data($data);
							//$this->zip->archive();
						}

						$this->zip->read_dir($goods_img_dir, FALSE);

						$success_cnt++; // 성공 시 카운트
					}
				}
			}
		}

		$zip_file = $this->_download_unique_zip_filename('danharoo_goods_supplier_img');
		$zip_path = $this->config->item('user_temp_image_dir').'/'.$zip_file;
		if(!$this->zip->archive($zip_path))
		{
			echo json_encode(array('info' => array('success' => false, 'text' => '다운로드 파일 생성에 실패했습니다.')), JSON_UNESCAPED_UNICODE);
			exit;
		}

		//$this->zip->download('autoscrap.goods.'.time().'.zip');
		//alert_close('다운로드가 완료되었습니다!');
		echo json_encode(array('info' => array('success' => true, 'text' => '선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다.', 'file' => $zip_file)), JSON_UNESCAPED_UNICODE);
	}

	// 상품 선택 이미지 압축 다운
	function goods_select_zip_down()
	{
		ini_set('memory_limit','-1');

		$this->load->library('zip');

		if(@$this->uri->segment(3))
		{
			$user_id = $this->session->userdata('user_id');
			$gb = $this->uri->segment(3);

			$this->load->helper('download');	// 다운로드 헬퍼로드

			//$data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_supplier_img.zip'); // Read the file's contents
			$zip_file = $this->_download_request_zip_filename('danharoo_goods_supplier_img', 'danharoo_goods_supplier_img.zip');
			$zip_path = $this->config->item('user_temp_image_dir').'/'.$zip_file;
			if(!is_file($zip_path))
				alert_close('다운로드 파일이 존재하지 않습니다!');

			$this->zip->read_file($zip_path);
			$name = urlencode('단하루상품이미지텍스트파일').'('.date('YmdHis', time()).').zip';

			$this->zip->download($name);
			//force_download($name, $data);
		}
		else
			alert_close('정상적인 접근이 아닙니다!');
	}

	// 상품이미지 폴더 관리
	function goods_img()
	{
		if($this->view_data['auth_code'] != 15)
			alert('정상적인 접근이 아닙니다!');

		if(!$this->uri->segment(3))
			alert_only('주소창 끝에 상품코드를 입력하세요!');

		$this->config->item('user_temp_image_dir', 1);

		$goods_code = $this->uri->segment(3);

		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code;

		$query = $this->db->query("SELECT id FROM goods_code WHERE gcode='{$goods_code}'");
		if($query->num_rows() == 0)
		{
			$insert_data = array(
						   'gcode' => $goods_code,
						   'created' => date('Y-m-d H:i:s', time())
						);

			$this->db->insert('goods_code', $insert_data);
		}

		//debug($goods_img_dir);

		if(!is_dir($goods_img_dir))
		{
			//debug($goods_img_dir);
			mkdir($goods_img_dir, 0777, TRUE);
		}

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/jquery.fileupload.css');
		$this->view_data['link_tag3'] = link_tag('/assets/css/jquery.fileupload-ui.css');
		$this->view_data['link_tag4'] = link_tag('/assets/css/ladda-themeless.min.css');
		$this->view_data['link_tag5'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->view_data['goods_code'] = $goods_code;

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_img.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품이미지 임시폴더 업로드
	function goods_img_upload()
	{
		$this->load->helper('cf_purge');
		$goods_code = $this->uri->segment(3);
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = '/data/files/goods/img/'.$goods_code.'/';
		//debug($goods_img_dir);

		$data = array(
			'upload_dir' => $goods_img_dir,
			'upload_url' => $goods_img_url,
			'accept_file_types' => '/\.(gif|jpe?g|png)$/i',
		);
		$this->load->library('upload_handler', $data);

		//debug($this->upload_handler);
		$goods_code = $this->input->get_post('goods_code');
		if (!empty($goods_code)) cf_purge_goods($goods_code);
	}

	// 상품이미지 임시폴더 관리
	function temp_img()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/jquery.fileupload.css');
		$this->view_data['link_tag3'] = link_tag('/assets/css/jquery.fileupload-ui.css');
		$this->view_data['link_tag4'] = link_tag('/assets/css/ladda-themeless.min.css');
		$this->view_data['link_tag5'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/temp_img.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품이미지 임시폴더 업로드
	function temp_upload()
	{
		$user_id = $this->session->userdata('user_id');

		$data = array(
			//'upload_dir' => '/home/hosting_users/danharoo2/www/data/files/goods/'.$user_id.'/',
			'accept_file_types' => '/\.(gif|jpe?g|png)$/i',
		);
		$this->load->library('upload_handler', $data);

		//debug($this->upload_handler);
	}

	// 엑셀 샘플 다운로드
	function excel_sample_down()
	{
		$user_id = $this->session->userdata('user_id');
		$sample_file = '/home/ubuntu/data/files/excel/product_sample_ver1.xls';

		if(file_exists($sample_file))
		{
			$this->load->helper('download');

			$down_file_name = 'AutoDaV1.0.'.time().'.xls';
			$sample_data = file_get_contents($sample_file);

			force_download($down_file_name, $sample_data);
		}
	}

	// 상품 자동 전송 관리
	function goods_cron()
	{

		$this->view_data['config'] = $this->config_vars;

		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag4'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		// 마켓목록
		$query = $this->db->query("SELECT market FROM user_market WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['markets'] = $query->result();

		// 전체상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['goods_all_cnt'] = $query->num_rows();

		// 선택상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' AND SendCheck='Y' ORDER BY id DESC");
		$this->view_data['goods_chk_cnt'] = $query->num_rows();
		//debug($this->view_data);

		// 일일 전송 상품수
		$query = $this->db->query("SELECT id FROM goods_cron WHERE user_id='{$this->view_data['user_id']}' AND SUBSTRING(created,1,10)='".date("Y-m-d", time())."' ORDER BY id DESC");
		$this->view_data['goods_day_cnt'] = $query->num_rows();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 조회
	function cron_master_ajax_list()
	{
		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods_cron WHERE user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$sql = "SELECT
					GS.*,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4
					FROM
						goods_cron			AS GS LEFT OUTER JOIN
						goods_image_cron	As GSI ON GS.id = GSI.goods_id
					WHERE
						user_id='{$user_id}'
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				$goods_delete_info = '';

				/*
				switch($row->market)
				{
					// 신상마켓
					case "G":
						$goods_delete_info = $this->sinsang_scrap->goods_delete_info($row->market, $row->GoodsNo);
						break;
				}

				if($goods_delete_info) $delete = 'N';
				else $delete = 'Y';
				*/

				if($row->activated == 1) $send = '수동';
				if($row->activated == 2) $send = '자동';

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$this->config_vars['open_market2'][$row->market],
													$send,
													$row->DelGb,
													$row->img0,
													$row->GoodsNo,
													$row->GoodsName,
													$row->GoodsPrice,
													$row->img1,
													$row->img2,
													$row->img3,
													$row->img4,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 조회
	function cron_ajax_list()
	{
		//debug($_GET);
		$GdsMstId = $this->uri->segment(3);
		$user_id = $this->session->userdata('user_id');

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$GdsMstId}' ORDER BY id ASC");

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsName,
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 리얼 상품 선택 삭제
	function goods_cron_select_erase()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($goodsId);exit;

		$deleting_cnt = count($goodsId); // 삭제할 상품수
		$deletede_cnt = 0; // 삭제된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				$rtn_json = $this->goods_cron_erase($goods_id, '1');
				$rtn_arr = json_decode($rtn_json, 1);
				if($rtn_arr['info']['success'])
					$deletede_cnt++; // 삭제 성공 시 카운트
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$deleting_cnt.' 개] 중 ['.$deletede_cnt.' 개] 상품이 삭제 되었습니다."}}';
	}

	// 상품개별삭제
	function goods_cron_erase($goods_id='', $rtnView='')
	{
		$user_id = $this->session->userdata('user_id');

		if(!$goods_id)
			$goods_id = $this->input->post('goodsId');

		//$goods_img_path = '/home/autoda/data/files/goods/'.$user_id.'/'.$goods_id.'/';

		if($goods_id > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_cron AS		GS LEFT OUTER JOIN
							goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image_cron	As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goods_id}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
				// 상품번호가 존재하면 해당 마켓별 상품 삭제 후 디비 삭제
				if($row->GoodsNo != '0')
				{
					//debug($row->market);exit;
					$rtn = $this->market_delete($row->market, trim($row->GoodsNo));
					//debug('rtn : '.$rtn);
				}

				if(isset($rtn) && $rtn)
				{
					$rtn_arr = json_decode($rtn, 1);
					if($rtn_arr['result'] == 'success')
					{
						$update_data = array(
									   'DelGb'	=> 'Y'
									);
						$this->db->where('id', $row->id);
						$this->db->update('goods_cron', $update_data);

						if($rtnView)
							return '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
						else
							echo '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
					}
					else
					{
						if($rtnView)
							return '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
						else
						{
							echo '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
							exit;
						}
					}
				}
				else
				{
					$update_data = array(
								   'DelGb'	=> 'Y'
								);
					$this->db->where('id', $row->id);
					$this->db->update('goods_cron', $update_data);

					if($rtnView)
						return '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
					else
						echo '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
				}
			}
			else
			{
				if($rtnView)
					return '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
				else
				{
					echo '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
					exit;
				}
			}
		}
	}

	// 리얼 상품 정보 조회
	function goods_cron_info()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($goodsId);exit;

		if($goodsId > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_cron AS		GS LEFT OUTER JOIN
							goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image_cron	As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goodsId}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();
			//debug($row);

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
				// 상품번호가 존재하면 해당 마켓별 상품 삭제 후 디비 삭제
				if($row->GoodsNo != '0')
				{
					$rtn = '';
					switch($row->market)
					{
						// 신상마켓
						case "G":
							$rtn = $this->sinsang_scrap->goods_delete_info($row->market, $row->GoodsNo);
							break;
					}

					if(!$rtn)
					{
						$update_data = array(
									   'DelGb'	=> 'Y'
									);
						$this->db->where('id', $row->id);
						$this->db->update('goods_cron', $update_data);

						echo '{"info":{"success":false,"text":"리얼 마켓에서 삭제된 상품입니다."}}';
					}
					else
						echo '{"info":{"success":true,"text":"리얼 마켓에서 존재하는 상품입니다."}}';
				}
			}
		}
	}

	function goods_error_log()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/extensions/Scroller/css/dataTables.scroller.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron_error_list.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 조회
	function goods_cron_error_log_ajax_list()
	{
		$v1_code_arr = $this->config->item('v1_code');
		//debug($v1_code_arr);

		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "VS.regdate";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		$common_from = '
			FROM
				v1_status	AS VS JOIN
				goods		AS GS ON VS.goods_id = GS.id JOIN
				goods_image	As GSI ON GS.id = GSI.goods_id
		';

		// 상품 전송 로그 목록
		$query = $this->db->query("SELECT count(VS.id) AS cnt {$common_from} WHERE VS.user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

		$sql = "SELECT
					VS.*,
					GS.*,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4
					{$common_from}
					WHERE
						VS.user_id='{$user_id}'
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//debug($sql);
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				//debug(count(explode('/', $row->GoodsImage)));
				if(count(explode('/', $row->GoodsImage)) > 1)
					$GoodsImage	= $row->GoodsImage;
					else
					{
						$GoodsImage	 = '/data/files/goods/'.$user_id.'/thumbnail/'.$row->GoodsImage;
						if($row->GoodsImage && !is_file($this->config->config['user_temp_image_dir'].$user_id.'/thumbnail/'.$row->GoodsImage))
						{
							$goods_code_image = $this->_goodscode_list_image($row->GoodsCode);
							if($goods_code_image) $GoodsImage = $goods_code_image;
						}
						else if(!$row->GoodsImage) $GoodsImage = $this->_goodscode_list_image($row->GoodsCode);
						$GoodsImage1 = ($row->img1)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img1:'';
						$GoodsImage2 = ($row->img2)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img2:'';
						$GoodsImage3 = ($row->img3)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img3:'';
						$GoodsImage4 = ($row->img4)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img4:'';
					}

				//if($row->SendCheck == 'Y') $SendCheck = '';

				$StInfo = json_decode($row->StInfo, 1);
				//$StInfo['resultMsg'] = $row->StInfo;
				if(!$StInfo) $StInfo['resultMsg'] = $row->StInfo;

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$row->GdsMstId,
													$row->SendCheck,
													$row->SendCount,
													$GoodsImage,
													$row->GoodsName,
													$row->StCode,
													$v1_code_arr[$row->StCode],
													$StInfo['resultMsg'],
													$row->regdate,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 자동 전송 관리 테스트
	function goods_cron_test()
	{

		$this->view_data['config'] = $this->config_vars;

		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag4'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		// 마켓목록
		$query = $this->db->query("SELECT market FROM user_market WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['markets'] = $query->result();

		// 전체상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['goods_all_cnt'] = $query->num_rows();

		// 선택상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' AND SendCheck='Y' ORDER BY id DESC");
		$this->view_data['goods_chk_cnt'] = $query->num_rows();
		//debug($this->view_data);

		// 일일 전송 상품수
		$query = $this->db->query("SELECT id FROM goods_cron WHERE user_id='{$this->view_data['user_id']}' AND SUBSTRING(created,1,10)='".date("Y-m-d", time())."' ORDER BY id DESC");
		$this->view_data['goods_day_cnt'] = $query->num_rows();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron_test', $this->view_data);
		$this->load->view('bottom');
	}

    // 상품다운로드현황
	function goods_down()
	{
        $this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag3'] = link_tag('/include/plugins/datepicker/datepicker3.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_down', $this->view_data);
		$this->load->view('bottom');
	}

    // 상품 다운로드 조회
	function goods_download_ajax_list()
	{
		$user_id = $this->session->userdata('user_id');

		// thumbnail 생성
		$this->load->library('image_lib');

		//debug($_REQUEST);

		$where = "";

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$cate1 = $this->input->post('cate1');
		$cate2 = $this->input->post('cate2');
		$cate3 = $this->input->post('cate3');

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;

				if($fieldName == 'GoodsName')
				{
					$fieldName = 'GoodsEtc5';
					$fieldData = strtoupper($fieldData);
				}
				if($fieldName == 'GoodsCode') $fieldData = strtolower($fieldData);

				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = '';
			//debug($where);
		}

		if($cate1)
		{
			$where .= " and Category1='$cate1' ";

			if($cate2)
			{
				$where .= " and Category2='$cate2' ";

				if($cate3)
					$where .= " and Category3='$cate3' ";
			}
		}

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("
									SELECT count(GDS.id) AS cnt
										FROM
                                            goods_down_status	AS GDS LEFT OUTER JOIN
                                            goods				AS GS	ON GDS.goods_id = GS.id LEFT OUTER JOIN
                                            goods_image			As GSI	ON GS.id = GSI.goods_id
										WHERE
											GDS.user_id = '{$user_id}'
											{$where}
								");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

		$sql = "SELECT
					GDS.id, GDS.gb, GDS.user_ip, GDS.regdate,
					GS.*,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9
					FROM
						goods_down_status     As GDS LEFT OUTER JOIN
						goods                 AS GS ON GDS.goods_id = GS.id LEFT OUTER JOIN
						goods_image	          As GSI ON GS.id = GSI.goods_id
					WHERE
						GDS.user_id = '{$user_id}'
						{$where}
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//debug($sql);
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				if(!$row->wishing) $row->wishing = 'N';

                $down_gb = down_gb_check($row->gb);

				$img_home = $this->config->config['user_temp_image_dir'].$row->user_id;	// 절대경로
				$img_path = '/data/files/goods/'.$row->user_id;								// 페이지경로
				//debug($img_home);

				//debug(count(explode('/', $row->GoodsImage)));
				if(count(explode('/', $row->GoodsImage)) > 1)
					$GoodsImage	= $row->GoodsImage;
					else
					{
						$GoodsImage	 = '/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->GoodsImage;
						if($row->GoodsImage && !is_file($this->config->config['user_temp_image_dir'].$row->user_id.'/thumbnail/'.$row->GoodsImage))
						{
							$goods_code_image = $this->_goodscode_list_image($row->GoodsCode);
							if($goods_code_image) $GoodsImage = $goods_code_image;
						}
						else if(!$row->GoodsImage) $GoodsImage = $this->_goodscode_list_image($row->GoodsCode);
						//$GoodsImage1 = ($row->img1)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img1:'';
					//$GoodsImage2 = ($row->img2)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img2:'';
					//$GoodsImage3 = ($row->img3)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img3:'';
					//$GoodsImage4 = ($row->img4)?'/data/files/goods/'.$user_id.'/thumbnail/'.$row->img4:'';

					for($j=0; $j<10; $j++)
					{
						$img_name = $row->{'img'.$j};
						if($img_name)
						{
							$config['source_image']	= $img_home.'/'.$img_name;
							$config['new_image']	= $img_home.'/thumbnail/';
							if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

							if(!is_file($config['new_image'].$img_name))
							{
								$config['image_library'] = 'gd2';
								$config['maintain_ratio'] = TRUE;
								$config['width']	= 100;
								$config['height']	= 100;
								//debug($config);
								$this->image_lib->initialize($config);
								$this->image_lib->resize();
							}
						}

						${'GoodsImage'.$j} = (is_file($config['new_image'].$img_name)) ? $img_path.'/thumbnail/'.$img_name : '';
					}
				}

				//if($row->SendCheck == 'Y') $SendCheck = '';

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$row->GdsMstId,
                                                    $down_gb,
													$row->GoodsCode,
													$GoodsImage,
													$row->GoodsEtc5,
													$row->GoodsEtc33,
													$row->GoodsPrice,
													$GoodsImage1,
													$GoodsImage2,
													$GoodsImage3,
													$GoodsImage4,
                                                    $row->user_ip,
													$row->regdate,
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

    // 배너 상품 관리
    function goods_banner()
	{
        $this->load->library('form_validation');

		$errors = array();
		//debug($this->session->all_userdata());
		//debug($this->input->post());

		$user_id = $this->session->userdata('user_id');
        $this->view_data['upload_path'] = $this->config->item('goods_banner_image_dir');
        $this->view_data['image_path'] = $this->config->item('user_temp_image_dir');
        $this->view_data['goodscode_img_path'] = $this->config->item('user_goodscode_img_dir');

		if($this->input->post())
		{
            //debug($_FILES);
			$this->form_validation->set_rules('goods_code', '상품코드', 'trim|required|xss_clean');
			$this->form_validation->set_rules('goods_order', '배너순서', 'trim|xss_clean'); // alpha_dash
			//$this->form_validation->set_rules('goods_banner', '배너이미지', 'trim|required|xss_clean');

			if ($this->form_validation->run())
			{
                $goods_code = $this->input->post('goods_code');
                $goods_order = $this->input->post('goods_order');

                $goods_query = $this->db->query("SELECT id, activated FROM goods WHERE GoodsCode='{$goods_code}'");
                $goods_data = $goods_query->row();
                //debug($goods_data);
                if(!$goods_data->id)
                    $this->view_data['error'] = '입력한 상품코드에 해당하는 상품이 없습니다!';
                else {
                    // if($goods_data->activated != 'Y')
                        // $this->view_data['error'] = '입력한 상품코드에 해당하는 상품은 비노출 상품입니다!';
                    // else {
                        $banner_query = $this->db->query("SELECT id FROM goods_banner WHERE user_id='{$user_id}' AND goods_id='".$goods_data->id."'");
                        $banner_data = $banner_query->row();
                        if($banner_data->id)
                            $this->view_data['error'] = '입력한 상품코드에 해당하는 상품은 이미 배너가 있습니다. 삭제 후 등록하세요!';
                        else {
                            // 상품코드별 이미지 배너확인(2018.02.01) 추가
                            $goods_banner_img = $this->config->item('user_goodscode_img_dir').$goods_code.'/'.$goods_code.'-newtalk.jpg';
                            if (!file_exists($goods_banner_img))
                            {
                                $config['upload_path'] = $this->view_data['upload_path'];
                        		$config['allowed_types'] = 'gif|jpg|png';
                        		$config['max_size']	= '0';
                        		$config['max_width']  = '0';
                        		$config['max_height']  = '0';
                                $config['max_filename']  = '0';
                                $config['encrypt_name']  = TRUE;

                        		$this->load->library('upload', $config);

                        		if ( ! $this->upload->do_upload('goods_banner') )
                        		{
                        			$this->view_data['error'] = $this->upload->display_errors('','');
                        		}
                                else
                                {
                                    $upload_data = array('upload_data' => $this->upload->data());
                                    //@chmod($this->view_data['upload_path'].$upload_data['upload_data']['file_name'], 0664);
                        			//debug($upload_data);
                                    $insert_data = array(
                                        'user_id'	=> $user_id,
                                        'goods_id'	=> $goods_data->id,
                                        'filename'	=> $upload_data['upload_data']['file_name'],
                                        'sort'	    => ($goods_order)?$goods_order:0,
                                        'set_ip'	=> $this->input->ip_address(),
                                        'created'	=> $this->info['created']
                                    );
                                    $this->db->insert('goods_banner', $insert_data);
                                }
                            }
                            else
                            {
                                //$destination_img = $this->config->item('goods_banner_image_dir').$goods_code.'-newtalk.jpg';
                                //if(move_uploaded_file($goods_banner_img, $destination_img))
                                //{
                                    $insert_data = array(
                                        'user_id'	=> $user_id,
                                        'goods_id'	=> $goods_data->id,
                                        'filename'	=> $goods_code.'-newtalk.jpg',
                                        'sort'	    => ($goods_order)?$goods_order:0,
                                        'set_ip'	=> $this->input->ip_address(),
                                        'created'	=> $this->info['created']
                                    );
                                    $this->db->insert('goods_banner', $insert_data);
                                //}
                                //else {
                                    //$this->view_data['error'] = '배너 파일을 복사하지 못했습니다.';
                                //}
                                # code...
                            }
                        }
                    // }
                }
			}
			//debug($this->form_validation);

			//if(!$this->input->post('market')) $this->error['market'] = 'market_select';
			//if(!$this->input->post('market_id')) $this->error['market_id'] = 'market_id_input';
			//if(!$this->input->post('market_pw')) $this->error['market_pw'] = 'market_pw_input';

			//foreach ($this->error as $k => $v) $this->view_data['errors'][$k] = $this->lang->line($v);

			if(is_array($this->form_validation->error_array()))	$errors = $this->form_validation->error_array();
		}

		$this->view_data['goods_code_error'] = '';

		if(count($errors) > 0)
		{
			foreach($errors AS $k => $v)
			{
				$this->view_data[$k.'_error'] = 'has-error';
			}
			//debug($errors);
		}

		$where = " AND GS.BrandName not in ('무드인더', '베러스윗') ";

        // 배너목록
        $sql = "SELECT
                    GSB.*,
                    GS.GoodsName, GS.GoodsCode, GS.GoodsEtc5
                    FROM
                        goods_banner  AS GSB LEFT OUTER JOIN
                        goods	      As GS ON GSB.goods_id = GS.id
                    WHERE GSB.user_id='{$user_id}'
					{$where}
                    ORDER BY GSB.id DESC
        ";

		$query = $this->db->query($sql);
		$this->view_data['my_list'] = $query->result();

		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_banner', $this->view_data);
		$this->load->view('bottom');
	}

    // 배너 상품 관리
    function goods_banner_delete()
	{
		//debug($this->session->all_userdata());
		//debug($this->input->post());

		$user_id = $this->session->userdata('user_id');
        $this->view_data['upload_path'] = $this->config->item('goods_banner_image_dir');

        $b_id = @$this->uri->segment(3);

		// 삭제할 배너 정보 확인
		$query = $this->db->query("SELECT id, filename FROM goods_banner WHERE user_id='{$user_id}' AND id='{$b_id}'");
		if($query->num_rows() == 1)
		{
			$row = $query->row();

			$this->db->where('id', $row->id);
			$this->db->delete('goods_banner');
			if($this->db->affected_rows())
            {
                @unlink($this->view_data['upload_path'].$row->filename);
				$data = '{"info":{"success":true,"text":"'.$this->view_data['upload_path'].$row->filename.'"}}';
            }
			else
				$data = '{"info":{"success":false,"text":"처리 과정에서 오류가 발생했습니다! 관리자에게 문의하세요."}}';
		}
		else
			$data = '{"info":{"success":false,"text":"삭제할 배너정보 확인 중 오류가 발생했습니다! 관리자에게 문의하세요."}}';

		echo $data;
    }

    // 도매미니몰 팝업 설정
    function popup_set()
	{
        $this->load->library('form_validation');

		$errors = array();
		//debug($this->session->all_userdata());
		//debug($this->input->post());

		$user_id = $this->session->userdata('user_id');
        $this->view_data['upload_path'] = $this->config->item('minimall_popup_image_dir').$user_id;
		$this->view_data['upload_url'] = $this->config->item('minimall_popup_image_url').$user_id.'/';

		if(!is_dir($this->view_data['upload_path']))
		{
			//debug($goods_img_dir);
			mkdir($this->view_data['upload_path'], 0777, TRUE);
		}

		if($this->input->post())
		{
            //debug($_FILES);
			$this->form_validation->set_rules('activated', '활성여부', 'trim|required|xss_clean');

			if ($this->form_validation->run())
			{
                $activated = $this->input->post('activated');

				$config['upload_path'] = $this->view_data['upload_path'];
				$config['allowed_types'] = 'gif|jpg|png|bmp';
				$config['max_size']	= '3072';
				$config['max_width']  = '0';
				$config['max_height']  = '0';
				$config['max_filename']  = '50';
				$config['encrypt_name']  = TRUE;

				$this->load->library('upload', $config);

				if ( ! $this->upload->do_upload('popup_file') )
				{
					$this->view_data['error'] = $this->upload->display_errors('','');
				}
				else
				{
					$upload_data = array('upload_data' => $this->upload->data());
					//@chmod($this->view_data['upload_path'].$upload_data['upload_data']['file_name'], 0664);
					//debug($upload_data);
					$insert_data = array(
						'user_id'	=> $user_id,
						'activated'	=> $activated,
						'filename'	=> $upload_data['upload_data']['file_name'],
						'set_ip'	=> $this->input->ip_address(),
						'created'	=> $this->info['created']
					);
					$this->db->insert('mall_popup', $insert_data);
				}
			}
			//debug($this->form_validation);

			//if(!$this->input->post('market')) $this->error['market'] = 'market_select';
			//if(!$this->input->post('market_id')) $this->error['market_id'] = 'market_id_input';
			//if(!$this->input->post('market_pw')) $this->error['market_pw'] = 'market_pw_input';

			//foreach ($this->error as $k => $v) $this->view_data['errors'][$k] = $this->lang->line($v);

			if(is_array($this->form_validation->error_array()))	$errors = $this->form_validation->error_array();
		}

		$this->view_data['goods_code_error'] = '';

		if(count($errors) > 0)
		{
			foreach($errors AS $k => $v)
			{
				$this->view_data[$k.'_error'] = 'has-error';
			}
			//debug($errors);
		}

        // 팝업목록
        $sql = "SELECT
                    MP.*
                    FROM
						mall_popup  AS MP
                    WHERE MP.user_id='{$user_id}'
                    ORDER BY MP.id DESC
        ";

		$query = $this->db->query($sql);
		$this->view_data['my_list'] = $query->result();

		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/popup_set', $this->view_data);
		$this->load->view('bottom');
	}

    // 도매미니몰 팝업 삭제
    function popup_set_delete()
	{
		//debug($this->session->all_userdata());
		//debug($this->input->post());

		$user_id = $this->session->userdata('user_id');
        $this->view_data['upload_path'] = $this->config->item('minimall_popup_image_dir').$user_id.'/';

        $b_id = @$this->uri->segment(3);

		// 삭제할 배너 정보 확인
		$query = $this->db->query("SELECT id, filename FROM mall_popup WHERE user_id='{$user_id}' AND id='{$b_id}'");
		if($query->num_rows() == 1)
		{
			$row = $query->row();

			$this->db->where('id', $row->id);
			$this->db->delete('mall_popup');
			if($this->db->affected_rows())
            {
                @unlink($this->view_data['upload_path'].$row->filename);
				$data = '{"info":{"success":true,"text":"'.$this->view_data['upload_path'].$row->filename.'"}}';
            }
			else
				$data = '{"info":{"success":false,"text":"처리 과정에서 오류가 발생했습니다! 관리자에게 문의하세요."}}';
		}
		else
			$data = '{"info":{"success":false,"text":"삭제할 배너정보 확인 중 오류가 발생했습니다! 관리자에게 문의하세요."}}';

		echo $data;
    }

	// 상품 재등록일 업데이트 반영(2021.11.26)
	function goods_created_set()
	{
		$this->load->model('goods_m');
		$user_id = $this->session->userdata('user_id');

		$goods_id = $this->input->post('goodsId');

		//$goods_img_path = '/home/autoda/data/files/goods/'.$user_id.'/'.$goods_id.'/';

		if($goods_id > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.GoodsEtc6
						FROM
							goods AS		GS LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goods_id}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();

			// 공급회원 상품이 맞는지 확인
			if($row->GoodsEtc6 == $this->view_data['username'])
			{
                $goods_data['re_created'] = $this->info['created'];

                $this->db->where('id', $goods_id);
                $this->db->update('goods', $goods_data);

				// 상품 액션(F:재등록일 수정) 로그
				$this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, 'G');

				echo '{"info":{"success":true,"text":"상품 재등록일이 수정되었습니다."}}';
			}
			else
				echo '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
		}
	}

	// 도매몰 상품이미지 썸네일 정보
	function get_goods_thumbnail()
	{
        $user_id = $this->session->userdata('user_id');
        $goods_id = $this->input->post('goodsId'); // 상품 일련번호

        if(!$user_id || !$goods_id) {
			echo '{"info":{"success":false,"text":"정상적인 접근(1)이 아닙니다."}}';
            exit;
        }

        // 등록 상품 정보 확인
        $sql = "SELECT GoodsCode FROM goods WHERE id='{$goods_id}'";
        $query = $this->db->query($sql);
        $row = $query->row();
        // debug($sql);exit;

        if(!$row->GoodsCode) {
			echo '{"info":{"success":false,"text":"정상적인 접근(2)이 아닙니다."}}';
            exit;
        }

	        $GoodsImage0 = '';
	        $GoodsImageWidth = '';
	        $GoodsImageHeight = '';
	        $thumb_name = '';
			foreach(array($row->GoodsCode.'-s_1.jpg', $row->GoodsCode.'_list1.jpg', $row->GoodsCode.'_list2.jpg', $row->GoodsCode.'_list3.jpg') as $candidate)
			{
				if(is_file($this->config->config['user_goodscode_img_dir'].$row->GoodsCode.'/'.$candidate))
				{
					$thumb_name = $candidate;
					break;
				}
			}
			if($thumb_name)
	        {
				$GoodsImage0 = 'https://newtalk.kr'.$this->config->config['user_goodscode_img_url'].$row->GoodsCode.'/'.$thumb_name;

	            $size = getimagesize($this->config->config['user_goodscode_img_dir'].$row->GoodsCode.'/'.$thumb_name);
	            // debug($size);
	            $GoodsImageWidth = $size[0];
	            $GoodsImageHeight = $size[1];
	        }
	        // debug($GoodsImage0);

			echo '{"info":{"success":true,"img":"'.$GoodsImage0.'","imgname":"'.$thumb_name.'","width":"'.$GoodsImageWidth.'","height":"'.$GoodsImageHeight.'"}}';
        exit;
	}

	// 도매몰 상품이미지 썸네일 정보
	function set_goods_thumbnail()
	{
        $goodsCode = $this->input->post('goodsCode');
        // debug($goodsCode);
        $croppedImage = $_FILES['croppedImage'];
        // debug($_FILES);

        if(!$goodsCode || $croppedImage['error'] !== 0)
        {
			echo '{"info":{"success":false,"text":"정상적인 접근(1)이 아닙니다."}}';
            exit;
        }

        $rtn = file_put_contents(
            $this->config->config['user_goodscode_img_dir'].$goodsCode.'/'.$goodsCode.'-s_1.jpg',
            fopen($croppedImage['tmp_name'], 'r')
        );
        // debug($rtn);

        if($rtn)
        {
			echo '{"info":{"success":true,"text":""}}';
            exit;
        } else {
			echo '{"info":{"success":false,"text":"저장 오류입니다."}}';
            exit;
        }
    }

	function test()
	{
		/*
		// 등록 상품 정보 확인
		$sql = "SELECT
					GS.*,
					GSD.Description, GSD.NoticeItemCodes,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
					FROM
						goods AS		GS LEFT OUTER JOIN
						goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
						goods_image		As GSI ON GS.id = GSI.goods_id
					WHERE GS.id='1'
		";
		$query = $this->db->query($sql);
		$row = $query->row();
		debug($row);
		*/

		$arr = array('a'=>1, 'b'=>2);
		$object = (object) $arr;
		//$arr = array('a','b','c');
		//debug($object);
		//echo "<BR><BR>";
		//$d = shuffle($arr);
		//var_dump($arr);

		$infos_json = '{"result":"fail","resultMsg":"\ub4f1\ub85d\ud560 \uc218 \uc5c6\ub294 \uc0c1\ud488\uba85\uc785\ub2c8\ub2e4. \uc0c1\ud45c\ubc95, \uc800\uc791\uad8c, \ud37c\ube14\ub9ac\uc2dc\ud2f0\uad8c \ub4f1\uc758 \uce68\ud574 \uc18c\uc9c0\uac00 \uc788\ub294 \uc0c1\ud488\uc744 \ub4f1\ub85d\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4."}';
		debug($infos_json);

		$resultArr = json_decode($infos_json, 1);
		debug($resultArr);

		$resultJson = json_encode($resultArr);
		debug($resultJson);

		$infos = iconv('UTF-8', 'EUC-KR', $infos_json);
		debug($infos);

		/*
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/extensions/Scroller/css/dataTables.scroller.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/test', $this->view_data);
		$this->load->view('bottom');
		*/
	}
}

/* End of file main.php */
/* Location: ./application/controllers/mian.php */
